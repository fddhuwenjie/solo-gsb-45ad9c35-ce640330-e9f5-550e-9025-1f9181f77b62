"""端到端冒烟测试：用 Flask test client 在临时库上跑完所有业务规则。
运行：PYTHONPATH=<site-packages> python3 smoke_test.py
"""
import os
import sys
import tempfile

os.environ["SCRIPT_DB"] = os.path.join(tempfile.mkdtemp(prefix="reh-"), "test.db")

import app as appmod

appmod.init_db()
client = appmod.app.test_client()

passed = []
def check(name, cond, extra=""):
    if not cond:
        print(f"FAIL: {name} {extra}")
        sys.exit(1)
    passed.append(name)

# 1. 初始工作稿
r = client.get("/api/draft").get_json()
check("seed revision=1", r["revision"] == 1, r["revision"])
check("seed 7 lines", len(r["lines"]) == 7, len(r["lines"]))
check("first cue is None", r["lines"][0]["cue"] is None)
check("cue = previous line", r["lines"][1]["cue"]["id"] == r["lines"][0]["id"])
check("seed note on #7", r["lines"][6]["note"] is not None
      and "停两拍" in r["lines"][6]["note"]["content"])
seed_ids = [l["id"] for l in r["lines"]]

# 角色页
r = client.get("/api/characters/林晚").get_json()
check("role page revision", r["revision"] == 1)
check("role only own lines", all(e is not None for e in r["entries"])
      and len(r["entries"]) == 3)
check("role cue previous speaker", r["entries"][0]["cue"] is None
      and r["entries"][1]["cue"]["character"] == "阿禾")
check("missing role 404", client.get("/api/characters/路人").status_code == 404)

# 已发布 r1
r = client.get("/api/publications").get_json()
check("pub list has r1", [p["revision"] for p in r["publications"]] == [1])
r = client.get("/api/publications/1").get_json()
check("pub r1 7 lines", len(r["lines"]) == 7)
check("pub r1 note frozen", r["lines"][6]["note"] is not None)
check("pub missing 404", client.get("/api/publications/99").status_code == 404)

# 2. 过期修订整次拒绝
resp = client.post("/api/draft", json={"revision": 0, "ops": [
    {"cid": "a", "type": "text", "target": {"type": "line", "id": seed_ids[0]},
     "text": "改了"}]})
check("stale draft -> 409", resp.status_code == 409, resp.status_code)
r = client.get("/api/draft").get_json()
check("stale changed nothing", r["revision"] == 1 and r["lines"][0]["id"] == seed_ids[0])

# 3. 改词：成功，修订 1->2，编号/笔记不变
resp = client.post("/api/draft", json={"revision": 1, "ops": [
    {"cid": "t1", "type": "text", "target": {"type": "line", "id": seed_ids[0]},
     "text": "（提着两盏马灯）老周，河面上那灯，是你挂的吗？"}]})
check("text edit 200", resp.status_code == 200, resp.get_json())
check("rev bumped to 2", resp.get_json()["revision"] == 2)
r = client.get("/api/draft").get_json()
check("text actually changed", "两盏马灯" in r["lines"][0]["text"])
check("id unchanged after text edit", [l["id"] for l in r["lines"]] == seed_ids)
check("note still on #7", r["lines"][6]["note"] is not None)

# r1 发布版不受影响
r = client.get("/api/publications/1").get_json()
check("pub r1 unaffected", "一盏马灯" in r["lines"][0]["text"])

# 4. 删除带笔记的台词 -> 整次拒绝 422，指出笔记
resp = client.post("/api/draft", json={"revision": 2, "ops": [
    {"cid": "d1", "type": "delete", "target": {"type": "line", "id": seed_ids[6]}}]})
check("delete noted line -> 422", resp.status_code == 422, resp.status_code)
body = resp.get_json()
check("conflict names line/character",
      body["conflicts"][0]["lineId"] == seed_ids[6]
      and body["conflicts"][0]["character"] == "林晚", body)
r = client.get("/api/draft").get_json()
check("rejected delete keeps line", len(r["lines"]) == 7 and r["revision"] == 2)

# 5. 改派带笔记的台词 -> 整次拒绝
resp = client.post("/api/draft", json={"revision": 2, "ops": [
    {"cid": "g1", "type": "assign", "target": {"type": "line", "id": seed_ids[6]},
     "character": "阿禾"}]})
check("assign noted line -> 422", resp.status_code == 422, resp.status_code)
check("assign conflict says new char",
      resp.get_json()["conflicts"][0]["newCharacter"] == "阿禾")

# 改派没有笔记的台词 -> 成功（重算提示时角色跟着词走）
resp = client.post("/api/draft", json={"revision": 2, "ops": [
    {"cid": "g2", "type": "assign", "target": {"type": "line", "id": seed_ids[0]},
     "character": "旁白"}]})
check("assign clean line 200", resp.status_code == 200, resp.get_json())
check("rev -> 3", resp.get_json()["revision"] == 3)
r = client.get("/api/characters/旁白").get_json()
check("new role page works", len(r["entries"]) == 1)

# 6. 批量：新增两句 + 删除无笔记的旧句 + 重排，整批一个修订；
#    并在同一批里把新台词作为后续 reorder 的锚点（前端也是这样生成操作的）
resp = client.post("/api/draft", json={"revision": 3, "ops": [
    {"cid": "add1", "type": "add", "character": "阿禾", "text": "新句 A（本批新增，随后被移动）",
     "anchor": None},
    {"cid": "add2", "type": "add", "character": "老周", "text": "新句 B（重排锚到本批新增的 A 之后）",
     "anchor": {"type": "new", "cid": "add1"}},
    {"cid": "del1", "type": "delete", "target": {"type": "line", "id": seed_ids[1]}},
    {"cid": "ord1", "type": "reorder", "target": {"type": "new", "cid": "add1"},
     "anchor": {"type": "line", "id": seed_ids[2]}},
    {"cid": "ord2", "type": "reorder", "target": {"type": "new", "cid": "add2"},
     "anchor": {"type": "new", "cid": "add1"}},
]})
check("batch 200", resp.status_code == 200, (resp.status_code, resp.get_json()))
new_ids = resp.get_json()["newIds"]
check("new ids returned", set(new_ids) == {"add1", "add2"}, new_ids)
check("rev -> 4", resp.get_json()["revision"] == 4)
r = client.get("/api/draft").get_json()
# 最终顺序：#1 旁白, #3 阿禾, A 阿禾, B 老周, #4, #5, #6, #7
pos = {l["id"]: i for i, l in enumerate(r["lines"])}
idA, idB = new_ids["add1"], new_ids["add2"]
check("add1 right after #3", pos[seed_ids[2]] == 1 and pos[idA] == 2, pos)
check("add2 right after add1", pos[idB] == pos[idA] + 1)
check("old #2 deleted", seed_ids[1] not in pos)
# 删除后编号不复用：再新增一句，编号必须更大
resp = client.post("/api/draft", json={"revision": 4, "ops": [
    {"cid": "add3", "type": "add", "character": "老周", "text": "新句 C",
     "anchor": None}]})
idC = resp.get_json()["newIds"]["add3"]
check("ids never reused", idC > max(seed_ids) and idC > idB and idC != seed_ids[1], idC)
check("rev -> 5", resp.get_json()["revision"] == 5)

# 提示随顺序重算
r = client.get("/api/draft").get_json()
check("cue recomputed after reorder",
      r["lines"][pos[idB]]["cue"]["id"] == idA)

# 7. 结构错误整批回滚：引用不存在的锚点，且同批有合法改动 -> 全不生效
resp = client.post("/api/draft", json={"revision": 5, "ops": [
    {"cid": "x1", "type": "text", "target": {"type": "line", "id": seed_ids[2]},
     "text": "这条合法改动也必须回滚"},
    {"cid": "x2", "type": "reorder", "target": {"type": "line", "id": seed_ids[2]},
     "anchor": {"type": "line", "id": 999999}},
]})
check("structural error -> 400", resp.status_code == 400, resp.status_code)
r = client.get("/api/draft").get_json()
check("rollback: rev still 5", r["revision"] == 5)
check("rollback: text untouched",
      all(l["id"] != seed_ids[2] or "回滚" not in l["text"] for l in r["lines"]))

# 8. 笔记：过期修订拒绝
resp = client.put(f"/api/lines/{seed_ids[6]}/note",
                  json={"revision": 1, "character": "林晚", "content": "过期保存"})
check("stale note -> 409", resp.status_code == 409, resp.status_code)

# 合法保存笔记 -> 修订 +1
resp = client.put(f"/api/lines/{seed_ids[6]}/note",
                  json={"revision": 5, "character": "林晚", "content": "更新后的笔记：灯亮三秒再开口"})
check("note save 200", resp.status_code == 200, resp.get_json())
check("note save bumps rev -> 6", resp.get_json()["revision"] == 6)
r = client.get("/api/characters/林晚").get_json()
note7 = [e for e in r["entries"] if e["lineId"] == seed_ids[6]][0]["note"]
check("note content updated", note7 and "灯亮三秒" in note7["content"])

# 清空笔记
resp = client.put(f"/api/lines/{seed_ids[6]}/note",
                  json={"revision": 6, "character": "林晚", "content": ""})
check("note clear -> 7", resp.get_json()["revision"] == 7)
r = client.get("/api/characters/林晚").get_json()
check("note cleared", [e for e in r["entries"] if e["lineId"] == seed_ids[6]][0]["note"] is None)

# 清空后删除该台词 -> 成功
resp = client.post("/api/draft", json={"revision": 7, "ops": [
    {"cid": "d2", "type": "delete", "target": {"type": "line", "id": seed_ids[6]}}]})
check("delete after note cleared 200", resp.status_code == 200, resp.get_json())
check("rev -> 8", resp.get_json()["revision"] == 8)

# 9. 发布 r2（当前修订 8）；之后再改工作稿，r2 不变
pub = client.post("/api/publications", json={"revision": 8, "label": "联排版"}).get_json()
check("publish r8", pub["revision"] == 8)
check("duplicate publish 422",
      client.post("/api/publications", json={"revision": 8}).status_code == 422)
check("publish stale 409",
      client.post("/api/publications", json={"revision": 7}).status_code == 409)

# 发布后继续改工作稿
resp = client.post("/api/draft", json={"revision": 8, "ops": [
    {"cid": "z1", "type": "add", "character": "旁白", "text": "发布之后才加的收场白",
     "anchor": None}]})
id_after = resp.get_json()["newIds"]["z1"]
r = client.get("/api/publications/8").get_json()
check("frozen r8 unaffected by later edit",
      all(l["id"] != id_after for l in r["lines"]) and len(r["lines"]) >= 6)
r = client.get("/api/draft").get_json()
check("draft has later edit", any(l["id"] == id_after for l in r["lines"]))
# 旧版 r1 仍可打开，且冻结着旧笔记
r1 = client.get("/api/publications/1").get_json()
check("r1 still open with original 7 + note",
      len(r1["lines"]) == 7 and r1["lines"][6]["note"] is not None
      and "停两拍" in r1["lines"][6]["note"]["content"])
# 已发布角色页
r = client.get("/api/publications/1/characters/林晚").get_json()
check("pub role page frozen notes",
      any(e["lineId"] == seed_ids[6] and e["note"] for e in r["entries"]))

# 10. 列表汇总
r = client.get("/api/publications").get_json()
check("pub list r1,r8", [p["revision"] for p in r["publications"]] == [1, 8])
check("working rev 9", r["workingRevision"] == 9)

# 11. 健康检查与首页
check("healthz", client.get("/healthz").data == b"ok")
check("index served", b"app.js" in client.get("/").data)
check("spa fallback serves index",
      b"app.js" in client.get("/some/spa/route").data)

print(f"\n全部 {len(passed)} 项冒烟检查通过 ✔")
for p in passed:
    print("  -", p)
