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

# 角色确认：示例三个角色在 r1 均已确认（随 r1 一起播种）
r = client.get("/api/confirmations").get_json()
check("seed all confirmed at r1",
      [(c["character"], c["status"]) for c in r["confirmations"]] ==
      [("林晚", "confirmed"), ("老周", "confirmed"), ("阿禾", "confirmed")]
      and all(c["confirmedRevision"] == 1 for c in r["confirmations"]), r)

# 角色页
r = client.get("/api/characters/林晚").get_json()
check("role page revision", r["revision"] == 1)
check("role only own lines", all(e is not None for e in r["entries"])
      and len(r["entries"]) == 3)
check("role cue previous speaker", r["entries"][0]["cue"] is None
      and r["entries"][1]["cue"]["character"] == "阿禾")
check("role page carries confirmation",
      r["confirmation"]["status"] == "confirmed"
      and r["confirmation"]["confirmedRevision"] == 1, r["confirmation"])
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

# 确认状态：改了林晚的第一句 -> 林晚（正文变）、老周（#2 的紧前提示变）待确认，
# 阿禾所见内容完全不受影响，仍是已确认
r = client.get("/api/confirmations").get_json()
st = {c["character"]: c["status"] for c in r["confirmations"]}
check("text edit: 林晚 pending", st["林晚"] == "pending", st)
check("text edit: 老周 cue pending", st["老周"] == "pending", st)
check("text edit: 阿禾 stays confirmed", st["阿禾"] == "confirmed", st)
# 待确认角色带回确认时所见快照（旧正文），已确认角色不带快照
lin = [c for c in r["confirmations"] if c["character"] == "林晚"][0]
check("pending snapshot has old content",
      lin["snapshot"] is not None and "提着一盏马灯" in lin["snapshot"][0]["text"]
      and lin["confirmedRevision"] == 1, lin)
ahe = [c for c in r["confirmations"] if c["character"] == "阿禾"][0]
check("confirmed snapshot omitted", ahe["snapshot"] is None)
# 过期修订确认被拒；角色不存在 404；缺 revision 400
check("stale confirm -> 409",
      client.post("/api/characters/林晚/confirmations", json={"revision": 1}).status_code == 409)
check("confirm unknown character -> 404",
      client.post("/api/characters/路人/confirmations", json={"revision": 2}).status_code == 404)
check("confirm without revision -> 400",
      client.post("/api/characters/林晚/confirmations", json={}).status_code == 400)
# 按当前修订确认 -> 201，且确认本身不推进修订号
resp = client.post("/api/characters/林晚/confirmations", json={"revision": 2})
check("confirm 201", resp.status_code == 201, resp.get_json())
check("confirm does not bump revision",
      client.get("/api/draft").get_json()["revision"] == 2)
# 再确认一次阿禾（内容没变，属于重复确认同样允许）
check("re-confirm unchanged role 201",
      client.post("/api/characters/阿禾/confirmations", json={"revision": 2}).status_code == 201)

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

# 9. 发布闸门：有待确认角色 -> 422 整次拒绝，且不留下任何（部分）发布版
r = client.get("/api/confirmations").get_json()
pending_now = {c["character"] for c in r["confirmations"] if c["status"] != "confirmed"}
check("four current characters at rev8",
      pending_now == {"旁白", "林晚", "老周", "阿禾"}, pending_now)
resp = client.post("/api/publications", json={"revision": 8, "label": "不该出现的版本"})
check("publish with pending -> 422", resp.status_code == 422, resp.status_code)
check("publish rejection lists characters",
      set(resp.get_json()["pendingCharacters"]) == pending_now, resp.get_json())
check("rejected publish leaves no snapshot",
      [p["revision"] for p in client.get("/api/publications").get_json()["publications"]] == [1])

# 逐个确认当前四个角色（确认不推进修订）；缺一个仍不能发布
for name in ["林晚", "老周", "阿禾"]:
    check(f"confirm {name} at rev8",
          client.post(f"/api/characters/{name}/confirmations",
                      json={"revision": 8}).status_code == 201)
resp = client.post("/api/publications", json={"revision": 8})
check("publish still blocked with one pending", resp.status_code == 422)
check("last pending character named",
      resp.get_json()["pendingCharacters"] == ["旁白"], resp.get_json())
check("confirm still no revision bump",
      client.get("/api/draft").get_json()["revision"] == 8)
check("confirm last character 旁白",
      client.post("/api/characters/旁白/confirmations", json={"revision": 8}).status_code == 201)

# 记录 r8 确认时阿禾 / 老周各句的紧前提示，供后续「无关改动」隔离比对
def cues_of(name):
    return [(e["lineId"], e["cue"]["id"] if e["cue"] else None)
            for e in client.get(f"/api/characters/{name}").get_json()["entries"]]
cues_at_rev8 = {"阿禾": cues_of("阿禾"), "老周": cues_of("老周")}

# 发布 r8（当前修订 8）；之后再改工作稿，r8 不变
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

# 无关改动不影响已确认：新增的收场白属于旁白，只有旁白的角色页多了一句
# （确认时它不存在）；其余角色所见完全不变，仍保持已确认
r = client.get("/api/confirmations").get_json()
st = {c["character"]: c["status"] for c in r["confirmations"]}
check("isolation: only line-owner role pending",
      st == {"旁白": "pending", "林晚": "confirmed", "老周": "confirmed",
             "阿禾": "confirmed"}, st)
# 角色页：新句在当前页里，但确认时快照中没有它（新增可见台词）
r = client.get("/api/characters/旁白").get_json()
check("new line visible now", any(e["lineId"] == id_after for e in r["entries"]))
check("new line absent from confirmed snapshot",
      all(s["lineId"] != id_after for s in r["confirmation"]["snapshot"]))
check("old line #1 still in snapshot as before",
      any(s["lineId"] == seed_ids[0] and s["cue"] is None
          for s in r["confirmation"]["snapshot"]))
check("confirm endpoint stale after new edit still 409",
      client.post("/api/characters/旁白/confirmations", json={"revision": 8}).status_code == 409)

# 确认状态持久化：重新 init_db（容器重启等价）后确认记录仍在（此时仍处 r9）
appmod.init_db()
r = client.get("/api/confirmations").get_json()
st = {c["character"]: c["status"] for c in r["confirmations"]}
check("confirmations survive restart",
      st == {"旁白": "pending", "林晚": "confirmed", "老周": "confirmed",
             "阿禾": "confirmed"}, st)
r = client.get("/api/publications").get_json()
check("pubs survive restart", [p["revision"] for p in r["publications"]] == [1, 8])

# 紧前提示隔离：旁白在 r9 重新确认，随后在开头插入一句新开场白（add + reorder，
# 与前端算法一致）-> 旁白第一句 #1 原本开场、紧前提示变为新句，待确认；
# 新句属于林晚，林晚也待确认；阿禾第一句 #3 的紧前提示始终是 #1，老周第一句
# 紧前提示始终是 #4，两人所见内容均未变化，保持已确认
check("confirm 旁白 at rev9",
      client.post("/api/characters/旁白/confirmations", json={"revision": 9}).status_code == 201)
resp = client.post("/api/draft", json={"revision": 9, "ops": [
    {"cid": "open1", "type": "add", "character": "林晚", "text": "新的开场白",
     "anchor": None},
    {"cid": "open2", "type": "reorder", "target": {"type": "new", "cid": "open1"},
     "anchor": None},
]})
check("insert top add+reorder 200", resp.status_code == 200, resp.get_json())
id_open = resp.get_json()["newIds"]["open1"]
r = client.get("/api/draft").get_json()
check("new opening is first", r["lines"][0]["id"] == id_open, r["lines"][0])
st = {c["character"]: c["status"] for c in
      client.get("/api/confirmations").get_json()["confirmations"]}
check("cue isolation: only 旁白/林晚 pending",
      st == {"旁白": "pending", "林晚": "pending", "老周": "confirmed",
             "阿禾": "confirmed"}, st)
# 旁白角色页明确标出 #1 的紧前提示从「无」变成新开场白
r = client.get("/api/characters/旁白").get_json()
e1 = next(e for e in r["entries"] if e["lineId"] == seed_ids[0])
check("旁白 #1 cue now the new opening", e1["cue"] is not None
      and e1["cue"]["id"] == id_open, e1)
s1 = next(s for s in r["confirmation"]["snapshot"] if s["lineId"] == seed_ids[0])
check("旁白 #1 snapshot cue was None", s1["cue"] is None)
# 阿禾、老周在 r10 仍已确认：记录 r8 确认后他们各句的紧前提示，开头插句后逐一比对不变
for who in ("阿禾", "老周"):
    r = client.get(f"/api/characters/{who}").get_json()
    check(f"{who} confirmed after top insert",
          r["confirmation"]["status"] == "confirmed", who)
    cues_now = [(e["lineId"], e["cue"]["id"] if e["cue"] else None) for e in r["entries"]]
    check(f"{who} cues unchanged by top insert", cues_now == cues_at_rev8[who],
          (cues_now, cues_at_rev8[who]))

# 10. 列表汇总
r = client.get("/api/publications").get_json()
check("pub list r1,r8", [p["revision"] for p in r["publications"]] == [1, 8])
check("working rev 10", r["workingRevision"] == 10)

# 11. 没有台词：确认清单为空，全员（零角色）视为可发布；旧版仍可打开
# 逐条删光工作稿（走到这里已无笔记；若有则先按原修订规则清空笔记）
while True:
    d = client.get("/api/draft").get_json()
    if not d["lines"]:
        break
    nr, lid, owner = d["revision"], d["lines"][0]["id"], d["lines"][0]["character"]
    first_entry = client.get(f"/api/characters/{owner}").get_json()["entries"][0]
    if first_entry["lineId"] == lid and first_entry["note"]:
        client.put(f"/api/lines/{lid}/note",
                   json={"revision": nr, "character": owner, "content": ""})
        nr += 1
    resp = client.post("/api/draft", json={"revision": nr, "ops": [
        {"cid": "emp", "type": "delete", "target": {"type": "line", "id": lid}}]})
    check("drain delete ok", resp.status_code == 200, (lid, resp.status_code, resp.get_json()))
r = client.get("/api/confirmations").get_json()
check("empty script: confirmation list empty", r["confirmations"] == [], r)
final_rev = client.get("/api/draft").get_json()["revision"]
check("empty role page 404", client.get("/api/characters/林晚").status_code == 404)
check("empty confirm unknown -> 404",
      client.post("/api/characters/林晚/confirmations",
                  json={"revision": final_rev}).status_code == 404)
resp = client.post("/api/publications", json={"revision": final_rev, "label": "空场"})
check("publish empty script allowed (no pending characters)",
      resp.status_code == 201, (resp.status_code, resp.get_json()))
check("empty publication snapshots zero lines",
      client.get(f"/api/publications/{final_rev}").get_json()["lines"] == [])
# 旧发布版仍然可打开
check("old publications still open after empty",
      [p["revision"] for p in client.get("/api/publications").get_json()["publications"]]
      == [1, 8, final_rev])

# 12. 健康检查与首页
check("healthz", client.get("/healthz").data == b"ok")
check("index served", b"app.js" in client.get("/").data)
check("spa fallback serves index",
      b"app.js" in client.get("/some/spa/route").data)

print(f"\n全部 {len(passed)} 项冒烟检查通过 ✔")
for p in passed:
    print("  -", p)
