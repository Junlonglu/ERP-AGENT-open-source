"""Prompt-boundary and real graph tests; no external API / Docker required."""
import asyncio
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pydantic import Field
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain.agents import create_agent
from langgraph.store.memory import InMemoryStore

from src.agent.progressive_skills import (
    SkillCatalog, ProgressiveSkillsMiddleware, attach_to_subagents, SLOT,
    safe_relative, metadata, CATALOG_LIMIT, DOCUMENT_LIMIT,
)


class RecordingModel(FakeMessagesListChatModel):
    seen: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class ProgressiveSkillsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "采购 技能" / "报价"
        self.directory.mkdir(parents=True)
        self.doc = self.directory / "SKILL.md"
        self.doc.write_text("---\nname: quote\ndescription: Compare quotes\n---\nBODY_MARKER\nRead references/cost.txt only if needed.", encoding="utf-8")
        (self.directory / "references").mkdir()
        (self.directory / "references/cost.txt").write_text("RESOURCE_MARKER", encoding="utf-8")
        self.store = InMemoryStore()
        self.catalog = SkillCatalog(self.root, self.store, "alice", sandbox=True)
        self.id = "builtin/采购 技能/报价/SKILL.md"
        self.tools = {t.name: t for t in self.catalog.tools()}

    def call(self, name, args):
        return json.loads(self.tools[name].invoke(args))

    def put(self, user="alice", key="my-skill/SKILL.md", text=None):
        text = text or "---\nname: private\ndescription: user description\n---\nPRIVATE_BODY"
        self.store.put(("persisted-skills", user), key, {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()})

    def test_pagination_error_keeps_the_reason(self):
        # 模型曾把 "Skill read unavailable (ValueError)" 当成技能坏了，
        # 连调 10 次 list_skills 换关键词；真实原因是 limit 越界。
        result = self.call("list_skills", {"limit": 99})

        self.assertFalse(result["success"])
        self.assertIn("Invalid pagination", result["error"])

    def test_storage_errors_stay_generic(self):
        with patch.object(SkillCatalog, "entries", side_effect=RuntimeError("mongo://user:pw@host")):
            result = self.call("list_skills", {})

        self.assertFalse(result["success"])
        self.assertNotIn("mongo://", result["error"])
        self.assertIn("check ID, path and file limits", result["error"])

    def test_index_is_metadata_only(self):
        self.assertIn("Compare quotes", self.catalog.index())
        self.assertNotIn("BODY_MARKER", self.catalog.index())
        self.assertNotIn("RESOURCE_MARKER", self.catalog.index())

    def test_selected_instruction_complete_and_resource_lazy(self):
        r = self.call("read_skill", {"skill_id": self.id})
        self.assertTrue(r["complete"])
        self.assertIn("BODY_MARKER", r["content"])
        self.assertNotIn("RESOURCE_MARKER", r["content"])
        self.assertIn("采购 技能", r["execution_path"])
        resource = self.call("read_skill_resource", {"skill_id": self.id, "path": "references/cost.txt"})
        self.assertEqual(resource["content"], "RESOURCE_MARKER")

    def test_pagination_and_invalid_ranges(self):
        a = self.call("read_skill_resource", {"skill_id": self.id, "path": "references/cost.txt", "max_chars": 5})
        b = self.call("read_skill_resource", {"skill_id": self.id, "path": "references/cost.txt", "offset": a["next_offset"]})
        self.assertEqual(a["content"]+b["content"], "RESOURCE_MARKER")
        self.assertFalse(self.call("list_skills", {"offset": -1})["success"])

    def test_paths_traversal_secrets_and_unknown(self):
        for path in ("../SKILL.md", "/etc/passwd", "a//b", "a/./b", "x\\y", "C:/file", ".env", "secret.key", "credentials.json"):
            with self.subTest(path=path):
                self.assertFalse(self.call("read_skill_resource", {"skill_id": self.id, "path": path})["success"])
        self.assertFalse(self.call("read_skill", {"skill_id": "builtin/../secret.md"})["success"])

    @unittest.skipIf(sys.platform == "win32", "Windows 创建符号链接需要管理员权限")
    def test_symlink_outside_root(self):
        outside = self.root / "outside.txt"
        outside.write_text("SECRET")
        (self.directory / "escape.txt").symlink_to(outside)
        self.assertFalse(self.call("read_skill_resource", {"skill_id": self.id, "path": "escape.txt"})["success"])

    def test_nested_skill_boundary(self):
        nested = self.directory / "nested"
        nested.mkdir()
        (nested/"SKILL.md").write_text("body")
        self.assertFalse(self.call("read_skill_resource", {"skill_id": self.id, "path": "nested/SKILL.md"})["success"])

    def test_user_isolation_and_origin_collision(self):
        self.put()
        self.put("bob", text="---\nname: BobOnly\ndescription: bob secret\n---\nBOB_BODY")
        index = self.catalog.index()
        self.assertIn("private", index)
        self.assertNotIn("PRIVATE_BODY", index)
        self.assertNotIn("bob secret", index)
        r = self.call("read_skill", {"skill_id": "user/my-skill/SKILL.md"})
        self.assertIn("PRIVATE_BODY", r["content"])
        self.assertNotIn("BOB_BODY", r["content"])

    def test_user_resources_and_same_catalog_new_install_update_delete(self):
        self.assertNotIn("private", self.catalog.index())
        self.put()
        self.put(key="my-skill/ref.txt", text="USER_RESOURCE")
        self.assertIn("private", self.catalog.index())
        r = self.call("read_skill_resource", {"skill_id": "user/my-skill/SKILL.md", "path": "ref.txt"})
        self.assertEqual(r["content"], "USER_RESOURCE")
        self.put(text="---\nname: newname\ndescription: updated\n---\nNEW_BODY")
        self.assertIn("newname", self.catalog.index())
        self.store.delete(("persisted-skills", "alice"), "my-skill/SKILL.md")
        self.assertNotIn("newname", self.catalog.index())
        self.assertFalse(self.call("read_skill", {"skill_id": "user/my-skill/SKILL.md"})["success"])

    def test_no_cache_so_rebuild_can_read_same_persisted_version(self):
        self.put()
        other = SkillCatalog(self.root, self.store, "alice", sandbox=True)
        self.assertEqual(self.catalog.read("user/my-skill/SKILL.md"), other.read("user/my-skill/SKILL.md"))

    def test_store_failure_leaves_builtin_available(self):
        self.catalog.store = Mock()
        self.catalog.store.search.side_effect = RuntimeError("do not expose secrets")
        text = self.catalog.index()
        self.assertIn("Compare quotes", text)
        self.assertIn("Store 暂不可用", text)
        self.assertNotIn("do not expose secrets", text)

    def test_store_search_paginates_beyond_default_limit(self):
        for i in range(102):
            self.put(key=f"s{i}/SKILL.md")
        entries, _ = self.catalog.entries()
        self.assertEqual(len(entries), 103)
        self.assertLessEqual(len(self.catalog.index()), CATALOG_LIMIT)
        a = self.call("list_skills", {"query": "user/", "limit": 30})
        self.assertEqual(a["total"], 102)
        self.assertEqual(a["next_offset"], 30)

    def test_no_frontmatter_body_leak_and_legacy_files(self):
        self.doc.write_text("BODY_MARKER", encoding="utf-8")
        (self.root/"reference.md").write_text("UNRELATED_BODY")
        (self.root/"legacy.md").write_text("---\nname: old\ndescription: legacy skill\n---\nLEGACY_BODY")
        index = self.catalog.index()
        self.assertNotIn("BODY_MARKER", index)
        self.assertNotIn("UNRELATED_BODY", index)
        self.assertNotIn("LEGACY_BODY", index)
        self.assertIn("legacy skill", index)
        self.assertFalse(self.call("read_skill", {"skill_id": "builtin/reference.md"})["success"])

    def test_oversized_document_fails_not_silent_truncation(self):
        self.doc.write_text("x"*(DOCUMENT_LIMIT+1))
        self.assertFalse(self.call("read_skill", {"skill_id": self.id})["success"])
        self.assertNotIn(self.id, self.catalog.index())

    def test_middleware_preserves_blocks_and_does_not_accumulate(self):
        request = Mock()
        request.system_message = SystemMessage(content=[{"type": "text", "text": "HEADER "+SLOT}, {"type": "text", "text": "OTHER"}])
        request.override.side_effect = lambda **kwargs: kwargs
        middleware = ProgressiveSkillsMiddleware(self.catalog)
        a = middleware.refresh(request)["system_message"]
        b = middleware.refresh(request)["system_message"]
        self.assertEqual(a.content, b.content)
        self.assertIn("OTHER", str(a.content))
        self.assertNotIn("BODY_MARKER", str(a.content))

    def test_real_graph_three_levels(self):
        model = RecordingModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "read_skill", "args": {"skill_id": self.id}, "id": "s1"}]),
            AIMessage(content="", tool_calls=[{"name": "read_skill_resource", "args": {"skill_id": self.id, "path": "references/cost.txt"}, "id": "s2"}]),
            AIMessage(content="done"),
        ])
        graph = create_agent(model=model, tools=list(self.tools.values()), system_prompt=SLOT,
                             middleware=[ProgressiveSkillsMiddleware(self.catalog)])
        graph.invoke({"messages": [HumanMessage(content="use quote skill")]})
        self.assertNotIn("BODY_MARKER", str(model.seen[0]))
        self.assertIn("BODY_MARKER", str(model.seen[1]))
        self.assertNotIn("RESOURCE_MARKER", str(model.seen[1]))
        self.assertIn("RESOURCE_MARKER", str(model.seen[2]))

    def test_async_real_graph(self):
        model = RecordingModel(responses=[AIMessage(content="done")])
        graph = create_agent(model=model, tools=list(self.tools.values()), system_prompt=SLOT,
                             middleware=[ProgressiveSkillsMiddleware(self.catalog)])
        result = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="hello")]}))
        self.assertEqual(result["messages"][-1].content, "done")

    def test_subagent_wiring(self):
        specs = [{"name": "worker", "system_prompt": "work", "tools": []}]
        attach_to_subagents(specs, self.catalog, list(self.tools.values()))
        self.assertIn(SLOT, specs[0]["system_prompt"])
        self.assertEqual(len(specs[0]["tools"]), 3)
        self.assertIsInstance(specs[0]["middleware"][0], ProgressiveSkillsMiddleware)

    def test_user_nested_skill_boundary(self):
        self.put()
        self.put(key="my-skill/nested/SKILL.md")
        self.put(key="my-skill/nested/ref.txt", text="NESTED")
        self.assertFalse(self.call("read_skill_resource", {
            "skill_id": "user/my-skill/SKILL.md", "path": "nested/ref.txt",
        })["success"])

    def test_real_deepagent_delegation(self):
        from deepagents import create_deep_agent
        from deepagents.backends import StateBackend
        child = RecordingModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "read_skill", "args": {"skill_id": self.id}, "id": "read-child"}]),
            AIMessage(content="child done"),
        ])
        parent = RecordingModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "task", "args": {"description": "Compare quotes", "subagent_type": "worker"}, "id": "delegate"}]),
            AIMessage(content="done"),
        ])
        specs = [{"name": "worker", "description": "Compare quotes", "system_prompt": "work", "tools": [], "model": child}]
        attach_to_subagents(specs, self.catalog, list(self.tools.values()))
        graph = create_deep_agent(model=parent, tools=list(self.tools.values()),
            system_prompt=SLOT, subagents=specs, skills=None,
            backend=StateBackend(),
            middleware=[ProgressiveSkillsMiddleware(self.catalog)])
        result = graph.invoke({"messages": [HumanMessage(content="delegate comparison")]})
        self.assertEqual(result["messages"][-1].content, "done")
        self.assertEqual(len(child.seen), 2)
        self.assertIn("Compare quotes", str(child.seen[0]))
        self.assertNotIn("BODY_MARKER", str(child.seen[0]))
        self.assertIn("BODY_MARKER", str(child.seen[1]))
        self.assertNotIn("BODY_MARKER", str(parent.seen))


if __name__ == "__main__":
    unittest.main()
