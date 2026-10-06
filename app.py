"""小剧场排练 · 剧本换版（Flask + SQLite + 原生 JavaScript）。

核心模型：
- 工作稿由一组有序台词组成，台词编号自增、永不复用（SQLite AUTOINCREMENT）。
- 每次修改工作稿（增删 / 改派 / 改词 / 重排）或保存笔记，工作稿修订号 +1。
- 修改必须附带「所见修订号」，过期则整次拒绝。
- 删除或改派仍带笔记的台词，整次拒绝并指出需先处理的笔记。
- 发布时把所有角色页与笔记冻结进同一修订，旧版始终可打开。
"""

import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, g, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DB_PATH = os.environ.get("SCRIPT_DB", os.path.join(BASE_DIR, "data", "rehearsal.db"))

MAX_TEXT = 2000          # 台词正文最大长度
MAX_NOTE = 2000          # 笔记最大长度
MAX_NAME = 60            # 角色名最大长度
MAX_OPS = 500            # 单次批量操作上限

# 初始化示例：一个原创的排练小场景（无版权问题）
SEED_CHARACTERS = ["林晚", "老周", "阿禾"]
SEED_LINES = [
    ("林晚", "（提着一盏马灯，压着嗓子）老周，河面上那盏灯，是你挂的吗？"),
    ("老周", "不是我。这三天夜里，它都在同一根桩子上，天一亮就灭。"),
    ("阿禾", "我去看过了。桩子下面没有脚印，只有一根新磨断的麻绳。"),
    ("林晚", "麻绳断口朝外……像是有人故意把什么东西，送进了雾里。"),
    ("老周", "都把绳子收了。今晚谁也不许单独下水，排练照常，灯，别去碰。"),
    ("阿禾", "（小声）可它今晚亮得比前两天都早。"),
    ("林晚", "那就让它亮着。戏排完之前，我们先当它——是自己人。"),
]
SEED_NOTE = (7, "这里停两拍再开口，马灯往下压，光打在麻绳上。")  # (台词编号, 示例笔记)


# --------------------------------------------------------------------------- #
# 数据库
# --------------------------------------------------------------------------- #
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit，手动开事务
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 5000")
        g.db.execute("PRAGMA journal_mode = WAL")
    return g.db


def init_db():
    """建表并写入初始示例（仅在空库时）。Docker 启动前只跑一次，避免多 worker 竞争。"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    first = not os.path.exists(DB_PATH)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- 台词编号：AUTOINCREMENT 保证编号永不复用（删除后也不会被新台词占用）
            CREATE TABLE IF NOT EXISTS line (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                character  TEXT NOT NULL,
                text       TEXT NOT NULL,
                pos        INTEGER NOT NULL,
                UNIQUE(pos)
            );
            CREATE INDEX IF NOT EXISTS idx_line_character ON line(character);

            -- 笔记挂在台词编号上；台词不在则笔记不存在（笔记永不随重排/改词落到别处）
            CREATE TABLE IF NOT EXISTS note (
                line_id    INTEGER PRIMARY KEY REFERENCES line(id),
                content    TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- 发布版：台词与笔记整体冻结，互不引用工作稿，工作稿后续修改不影响旧版
            CREATE TABLE IF NOT EXISTS publication (
                revision   INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                label      TEXT
            );
            CREATE TABLE IF NOT EXISTS pub_line (
                id         INTEGER NOT NULL,          -- 冻结时的原台词编号
                revision   INTEGER NOT NULL,
                character  TEXT NOT NULL,
                text       TEXT NOT NULL,
                pos        INTEGER NOT NULL,
                PRIMARY KEY (revision, id)
            );
            CREATE INDEX IF NOT EXISTS idx_pub_rev_char ON pub_line(revision, character);
            CREATE TABLE IF NOT EXISTS pub_note (
                revision   INTEGER NOT NULL,
                line_id    INTEGER NOT NULL,
                character  TEXT NOT NULL,
                content    TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (revision, line_id)
            );
            """
        )
        if first:
            _seed(conn)
    finally:
        conn.close()


def _seed(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = utcnow()
        for i, (char, text) in enumerate(SEED_LINES):
            cur = conn.execute(
                "INSERT INTO line (character, text, pos) VALUES (?, ?, ?)",
                (char, text, i),
            )
            line_id = cur.lastrowid
            if line_id == SEED_NOTE[0]:
                conn.execute(
                    "INSERT INTO note (line_id, content, updated_at) VALUES (?, ?, ?)",
                    (line_id, SEED_NOTE[1], now),
                )
        conn.execute("INSERT INTO meta (key, value) VALUES ('working_revision', '1')")
        # 工作稿 1 修订：示例工作稿就是修订 1；直接冻结发布为 r1
        conn.execute("INSERT INTO publication (revision, created_at, label) VALUES (?, ?, ?)",
                     (1, now, "初版（示例）"))
        conn.execute(
            "INSERT INTO pub_line (id, revision, character, text, pos) "
            "SELECT id, 1, character, text, pos FROM line"
        )
        conn.execute(
            "INSERT INTO pub_note (revision, line_id, character, content, updated_at) "
            "SELECT 1, n.line_id, l.character, n.content, n.updated_at "
            "FROM note n JOIN line l ON l.id = n.line_id"
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
def draft_payload(conn):
    """工作稿 + 每句紧前一句提示。提示按当前顺序即时计算，顺序变了提示自动重算。"""
    rows = conn.execute("SELECT id, character, text, pos FROM line ORDER BY pos").fetchall()
    notes = {
        r["line_id"]: {"content": r["content"], "updatedAt": r["updated_at"]}
        for r in conn.execute("SELECT line_id, content, updated_at FROM note")
    }
    lines = []
    prev = None
    for r in rows:
        lines.append({
            "id": r["id"],
            "character": r["character"],
            "text": r["text"],
            "note": notes.get(r["id"]),
            "cue": prev,  # 紧前一句：第一句为 None
        })
        prev = {"id": r["id"], "character": r["character"], "text": r["text"]}
    return {
        "revision": int(get_meta(conn, "working_revision", "0")),
        "characters": sorted({r["character"] for r in rows}),
        "lines": lines,
    }


def publications_payload(conn):
    pubs = conn.execute(
        "SELECT revision, created_at, label FROM publication ORDER BY revision"
    ).fetchall()
    out = []
    for p in pubs:
        rev = p["revision"]
        chars = [
            r["character"]
            for r in conn.execute(
                "SELECT DISTINCT character FROM pub_line WHERE revision = ? ORDER BY character",
                (rev,),
            )
        ]
        count = conn.execute(
            "SELECT COUNT(*) c FROM pub_line WHERE revision = ?", (rev,)
        ).fetchone()["c"]
        notes = conn.execute(
            "SELECT COUNT(*) c FROM pub_note WHERE revision = ?", (rev,)
        ).fetchone()["c"]
        out.append({
            "revision": rev,
            "createdAt": p["created_at"],
            "label": p["label"],
            "characters": chars,
            "lineCount": count,
            "noteCount": notes,
        })
    return out


# --------------------------------------------------------------------------- #
# 工作稿批量修改
# --------------------------------------------------------------------------- #
def apply_ops(conn, ops, expected_rev):
    """在一个事务内按序应用一批操作；任一失败则整次回滚（绝不部分生效）。

    操作（全部携带前端临时编号 cid，便于报错定位）：
      add:      新增台词，落在 anchor 之后（anchor 为 None 则置顶）
      delete:   删除台词（带笔记则拒绝）
      assign:   改派角色（带笔记则拒绝，避免笔记落到别的角色）
      text:     修改正文（编号与笔记都不变）
      reorder:  把 move 台词移到 anchor 之后
    新台词可被后续操作引用，引用键为 "n:<add 的 cid>"。
    """
    errors = []       # 结构 / 参数错误（400）
    conflicts = []    # 业务冲突：过期修订、笔记阻挡（422 / 409）

    current_rev = int(get_meta(conn, "working_revision", "0"))
    if expected_rev != current_rev:
        conflicts.append({
            "type": "stale",
            "expected": current_rev,
            "received": expected_rev,
            "message": f"工作稿已更新到修订 {current_rev}，你提交的是修订 {expected_rev}，"
                       "本次修改整次拒绝，请刷新查看其他页面的改动后重试。",
        })
        return None, errors, conflicts

    if not isinstance(ops, list) or not ops:
        errors.append({"message": "ops 必须是非空操作数组"})
        return None, errors, conflicts
    if len(ops) > MAX_OPS:
        errors.append({"message": f"单次最多 {MAX_OPS} 个操作"})
        return None, errors, conflicts

    # 载入当前工作稿到内存做干跑（dry run）
    rows = conn.execute("SELECT id, character, text, pos FROM line ORDER BY pos").fetchall()
    items = [{"key": f"l{r['id']}", "id": r["id"], "new": False,
              "character": r["character"], "text": r["text"]} for r in rows]
    noted = {r["line_id"] for r in conn.execute("SELECT line_id FROM note")}

    valid_types = {"add", "delete", "assign", "text", "reorder"}

    def find(key):
        for idx, it in enumerate(items):
            if it["key"] == key:
                return idx, it
        return -1, None

    def key_of(ref):
        """把操作里的目标引用解析成 items 键；返回 None 表示引用非法（已记 error）。"""
        if ref is None:
            return None
        if not isinstance(ref, dict):
            errors.append({"cid": None, "message": "引用必须是 {type, id/cid}"})
            return None
        if ref.get("type") == "line":
            return f"l{ref.get('id')}"
        if ref.get("type") == "new":
            return f"n:{ref.get('cid')}"
        errors.append({"cid": None, "message": f"未知引用类型: {ref.get('type')!r}"})
        return None

    seq = 1
    for op in ops:
        cid = op.get("cid") if isinstance(op, dict) else None
        if not isinstance(op, dict) or op.get("type") not in valid_types:
            errors.append({"cid": cid, "message": "操作类型无效"})
            continue
        typ = op["type"]

        if typ == "add":
            char = (op.get("character") or "").strip()
            text = (op.get("text") or "").strip()
            if not char or len(char) > MAX_NAME:
                errors.append({"cid": cid, "message": "角色名必填且长度需合法"})
                continue
            if not text or len(text) > MAX_TEXT:
                errors.append({"cid": cid, "message": f"台词正文必填且不超过 {MAX_TEXT} 字"})
                continue
            anchor_key = key_of(op.get("anchor"))
            if anchor_key is not None:
                idx, _ = find(anchor_key)
                if idx < 0:
                    errors.append({"cid": cid, "message": "定位锚点不存在或已删除"})
                    continue
            items.append({"key": f"n:{cid}", "id": None, "new": True,
                          "character": char, "text": text,
                          "anchor": anchor_key})  # anchor=None => 置顶
            seq += 1

        elif typ == "delete":
            tkey = key_of(op.get("target"))
            idx, it = find(tkey) if tkey else (-1, None)
            if it is None:
                errors.append({"cid": cid, "message": "要删除的台词不存在或已删除"})
                continue
            if not it["new"] and it["id"] in noted:
                conflicts.append({
                    "type": "note_block",
                    "action": "delete",
                    "lineId": it["id"],
                    "character": it["character"],
                    "message": f"台词 #{it['id']}（{it['character']}）仍有笔记，"
                               "请先到该角色页清空或改写笔记，再删除。",
                })
                continue
            items.pop(idx)

        elif typ == "assign":
            tkey = key_of(op.get("target"))
            idx, it = find(tkey) if tkey else (-1, None)
            if it is None:
                errors.append({"cid": cid, "message": "要改派的台词不存在或已删除"})
                continue
            char = (op.get("character") or "").strip()
            if not char or len(char) > MAX_NAME:
                errors.append({"cid": cid, "message": "新角色名必填且长度需合法"})
                continue
            if not it["new"] and it["id"] in noted and char != it["character"]:
                conflicts.append({
                    "type": "note_block",
                    "action": "assign",
                    "lineId": it["id"],
                    "character": it["character"],
                    "newCharacter": char,
                    "message": f"台词 #{it['id']}（{it['character']}）仍有笔记，"
                               f"不能改派给「{char}」，否则笔记会落到别的角色。"
                               "请先到该角色页清空或改写笔记。",
                })
                continue
            it["character"] = char

        elif typ == "text":
            tkey = key_of(op.get("target"))
            idx, it = find(tkey) if tkey else (-1, None)
            if it is None:
                errors.append({"cid": cid, "message": "要修改的台词不存在或已删除"})
                continue
            text = (op.get("text") or "").strip()
            if not text or len(text) > MAX_TEXT:
                errors.append({"cid": cid, "message": f"台词正文必填且不超过 {MAX_TEXT} 字"})
                continue
            it["text"] = text

        else:  # reorder
            tkey = key_of(op.get("target"))
            m_idx, it = find(tkey) if tkey else (-1, None)
            if it is None:
                errors.append({"cid": cid, "message": "要移动的台词不存在或已删除"})
                continue
            anchor_key = key_of(op.get("anchor"))
            if anchor_key == tkey:
                errors.append({"cid": cid, "message": "不能以台词自身作为定位锚点"})
                continue
            a_idx = -1
            if anchor_key is not None:
                a_idx, anchor = find(anchor_key)
                if anchor is None:
                    errors.append({"cid": cid, "message": "定位锚点不存在或已删除"})
                        # 注意：锚点若是本批新增台词，它也必须仍在列表中
                    continue
            items.pop(m_idx)
            # 弹出后锚点下标可能前移一位
            if anchor_key is None:
                items.insert(0, it)
            else:
                a_idx, _ = find(anchor_key)
                items.insert(a_idx + 1, it)

    if errors or conflicts:
        return None, errors, conflicts

    # ---------------- 干跑通过：进入事务，真正落库 ---------------- #
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 二次确认修订（拿到写锁后），杜绝并发下的覆盖
        locked_rev = int(get_meta(conn, "working_revision", "0"))
        if locked_rev != expected_rev:
            conn.execute("ROLLBACK")
            conflicts.append({
                "type": "stale",
                "expected": locked_rev,
                "received": expected_rev,
                "message": f"工作稿已更新到修订 {locked_rev}，本次修改整次拒绝，请刷新后重试。",
            })
            return None, errors, conflicts

        id_map = {}  # 前端 cid -> 新建台词的真实编号

        # 先插入本批新增台词（编号永不复用），临时位置给互不相同的大值，稍后统一重排
        for idx_new, it in enumerate(items):
            if it["new"]:
                cur = conn.execute(
                    "INSERT INTO line (character, text, pos) VALUES (?, ?, ?)",
                    (it["character"], it["text"], 10 ** 9 + idx_new),
                )
                it["id"] = cur.lastrowid
                it["new"] = False
                id_map[it["key"][2:]] = it["id"]  # 去掉 "n:" 前缀得到 cid

        # 按 items 的最终顺序重写 pos；被 delete 的已不在 items 中
        surviving_ids = {it["id"] for it in items}
        placeholders = ",".join("?" for _ in surviving_ids) if surviving_ids else ""
        old_ids = [
            r["id"] for r in conn.execute("SELECT id FROM line").fetchall()
            if r["id"] not in surviving_ids
        ]
        for old_id in old_ids:
            # 能走到这里说明无笔记（干跑已拦）；FK 开着，双保险
            conn.execute("DELETE FROM line WHERE id = ?", (old_id,))
        # 先把存活行整体挪到互不冲突的临时大位置，再落最终位置，
        # 避免逐行更新时两行的 pos 瞬时相同而触发 UNIQUE(pos)
        for tmp, it in enumerate(items):
            conn.execute("UPDATE line SET pos = ? WHERE id = ?",
                         (-(tmp + 1), it["id"]))
        for pos, it in enumerate(items):
            conn.execute(
                "UPDATE line SET character = ?, text = ?, pos = ? WHERE id = ?",
                (it["character"], it["text"], pos, it["id"]),
            )

        new_rev = expected_rev + 1
        set_meta(conn, "working_revision", new_rev)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return {"revision": new_rev, "newIds": id_map}, [], []


# --------------------------------------------------------------------------- #
# Flask 应用
# --------------------------------------------------------------------------- #
def create_app():
    app = Flask(__name__, static_folder=None)
    app.url_map.strict_slashes = False

    @app.teardown_appcontext
    def close_db(exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "not_found", "message": "资源不存在"}), 404
        return send_from_directory(STATIC_DIR, "index.html")

    def bad_request(message):
        return jsonify({"error": "bad_request", "message": message}), 400

    # ---- 工作稿 ---- #
    @app.get("/api/draft")
    def get_draft():
        return jsonify(draft_payload(get_db()))

    @app.post("/api/draft")
    def update_draft():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return bad_request("请求体必须是 JSON 对象")
        rev = body.get("revision")
        if not isinstance(rev, int) or isinstance(rev, bool):
            return bad_request("revision 必须是整数（你所见的工作稿修订号）")
        result, errors, conflicts = apply_ops(get_db(), body.get("ops"), rev)
        if errors or conflicts:
            stale = next((c for c in conflicts if c.get("type") == "stale"), None)
            if stale:
                return jsonify({"error": "stale_revision", **stale}), 409
            if conflicts:
                return jsonify({"error": "note_blocked",
                                "message": "整次修改被拒绝，请先处理下列笔记。",
                                "conflicts": conflicts,
                                "details": errors}), 422
            return jsonify({"error": "bad_request",
                            "message": "操作数据有误，整次修改未生效。",
                            "details": errors}), 400
        return jsonify(result)

    # ---- 角色页（工作稿）---- #
    @app.get("/api/characters/<path:name>")
    def get_character(name):
        conn = get_db()
        rows = conn.execute(
            "SELECT id, text, pos FROM line WHERE character = ? ORDER BY pos", (name,)
        ).fetchall()
        if not rows:
            return jsonify({"error": "not_found",
                            "message": f"角色「{name}」在工作稿中没有台词"}), 404
        entries = []
        for r in rows:
            cue = conn.execute(
                "SELECT id, character, text FROM line WHERE pos < ? ORDER BY pos DESC LIMIT 1",
                (r["pos"],),
            ).fetchone()
            note = conn.execute(
                "SELECT content, updated_at FROM note WHERE line_id = ?", (r["id"],)
            ).fetchone()
            entries.append({
                "lineId": r["id"],
                "text": r["text"],
                "cue": dict(cue) if cue else None,
                "note": {"content": note["content"], "updatedAt": note["updated_at"]}
                        if note else None,
            })
        return jsonify({
            "character": name,
            "revision": int(get_meta(conn, "working_revision", "0")),
            "entries": entries,
        })

    # ---- 笔记：保存在自己角色的台词上；同样要带所见修订号 ---- #
    @app.put("/api/lines/<int:line_id>/note")
    def put_note(line_id):
        conn = get_db()
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return bad_request("请求体必须是 JSON 对象")
        rev = body.get("revision")
        if not isinstance(rev, int) or isinstance(rev, bool):
            return bad_request("revision 必须是整数（你所见的工作稿修订号）")
        content = body.get("content")
        if content is not None:
            content = str(content).strip()
        current_rev = int(get_meta(conn, "working_revision", "0"))
        if rev != current_rev:
            return jsonify({
                "error": "stale_revision", "type": "stale",
                "expected": current_rev, "received": rev,
                "message": f"工作稿已更新到修订 {current_rev}，笔记未保存，"
                           "请刷新（页面会按新顺序重新计算提示）后重试。",
            }), 409

        line = conn.execute(
            "SELECT id, character FROM line WHERE id = ?", (line_id,)
        ).fetchone()
        if line is None:
            return jsonify({"error": "not_found",
                            "message": f"台词 #{line_id} 已不在工作稿中"}), 404
        if body.get("character") and body["character"] != line["character"]:
            # 防止把 A 角色页的笔记写到改派后的 B 角色名下（正常 UI 不会触发）
            return jsonify({"error": "character_mismatch",
                            "message": f"台词 #{line_id} 现属于「{line['character']}」，"
                                       "请刷新角色页。"}), 422

        conn.execute("BEGIN IMMEDIATE")
        try:
            locked_rev = int(get_meta(conn, "working_revision", "0"))
            if locked_rev != rev:
                conn.execute("ROLLBACK")
                return jsonify({"error": "stale_revision", "type": "stale",
                                "expected": locked_rev, "received": rev,
                                "message": "工作稿刚被他人更新，笔记未保存，请刷新后重试。"}), 409
            now = utcnow()
            if content:
                if len(content) > MAX_NOTE:
                    conn.execute("ROLLBACK")
                    return bad_request(f"笔记不超过 {MAX_NOTE} 字")
                conn.execute(
                    "INSERT INTO note (line_id, content, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(line_id) DO UPDATE SET content = excluded.content, "
                    "updated_at = excluded.updated_at",
                    (line_id, content, now),
                )
            else:
                conn.execute("DELETE FROM note WHERE line_id = ?", (line_id,))
            new_rev = rev + 1
            set_meta(conn, "working_revision", new_rev)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return jsonify({"revision": new_rev, "lineId": line_id,
                        "character": line["character"],
                        "content": content or None, "updatedAt": now if content else None})

    # ---- 发布：把所有角色页与笔记冻结到同一修订 ---- #
    @app.post("/api/publications")
    def publish():
        conn = get_db()
        body = request.get_json(silent=True) or {}
        rev = body.get("revision")
        label = (body.get("label") or "").strip() or None
        if not isinstance(rev, int) or isinstance(rev, bool):
            return bad_request("revision 必须是整数（你所见的工作稿修订号）")
        if label and len(label) > 80:
            return bad_request("版本说明不超过 80 字")
        conn.execute("BEGIN IMMEDIATE")
        try:
            current_rev = int(get_meta(conn, "working_revision", "0"))
            if rev != current_rev:
                conn.execute("ROLLBACK")
                return jsonify({
                    "error": "stale_revision", "type": "stale",
                    "expected": current_rev, "received": rev,
                    "message": f"工作稿已更新到修订 {current_rev}，发布整次拒绝，请刷新后重试。",
                }), 409
            exists = conn.execute(
                "SELECT 1 FROM publication WHERE revision = ?", (rev,)
            ).fetchone()
            if exists:
                conn.execute("ROLLBACK")
                return jsonify({"error": "already_published",
                                "message": f"修订 {rev} 已发布过，请查看已发布版。"}), 422
            now = utcnow()
            conn.execute(
                "INSERT INTO publication (revision, created_at, label) VALUES (?, ?, ?)",
                (rev, now, label),
            )
            conn.execute(
                "INSERT INTO pub_line (id, revision, character, text, pos) "
                "SELECT id, ?, character, text, pos FROM line", (rev,)
            )
            conn.execute(
                "INSERT INTO pub_note (revision, line_id, character, content, updated_at) "
                "SELECT ?, n.line_id, l.character, n.content, n.updated_at "
                "FROM note n JOIN line l ON l.id = n.line_id", (rev,)
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return jsonify({"revision": rev, "createdAt": now, "label": label}), 201

    @app.get("/api/publications")
    def list_publications():
        return jsonify({"workingRevision": int(get_meta(get_db(), "working_revision", "0")),
                        "publications": publications_payload(get_db())})

    @app.get("/api/publications/<int:rev>")
    def get_publication(rev):
        conn = get_db()
        pub = conn.execute(
            "SELECT revision, created_at, label FROM publication WHERE revision = ?", (rev,)
        ).fetchone()
        if pub is None:
            return jsonify({"error": "not_found", "message": f"修订 r{rev} 未发布"}), 404
        rows = conn.execute(
            "SELECT id, character, text, pos FROM pub_line WHERE revision = ? ORDER BY pos",
            (rev,),
        ).fetchall()
        notes = {
            r["line_id"]: {"content": r["content"], "updatedAt": r["updated_at"],
                           "character": r["character"]}
            for r in conn.execute(
                "SELECT line_id, character, content, updated_at FROM pub_note WHERE revision = ?",
                (rev,),
            )
        }
        lines, prev = [], None
        for r in rows:
            lines.append({"id": r["id"], "character": r["character"], "text": r["text"],
                          "note": notes.get(r["id"]), "cue": prev})
            prev = {"id": r["id"], "character": r["character"], "text": r["text"]}
        return jsonify({"revision": pub["revision"], "createdAt": pub["created_at"],
                        "label": pub["label"],
                        "characters": sorted({r["character"] for r in rows}),
                        "lines": lines})

    @app.get("/api/publications/<int:rev>/characters/<path:name>")
    def get_pub_character(rev, name):
        conn = get_db()
        pub = conn.execute(
            "SELECT label FROM publication WHERE revision = ?", (rev,)
        ).fetchone()
        if pub is None:
            return jsonify({"error": "not_found", "message": f"修订 r{rev} 未发布"}), 404
        rows = conn.execute(
            "SELECT id, text, pos FROM pub_line WHERE revision = ? AND character = ? ORDER BY pos",
            (rev, name),
        ).fetchall()
        if not rows:
            return jsonify({"error": "not_found",
                            "message": f"r{rev} 中角色「{name}」没有台词"}), 404
        entries = []
        for r in rows:
            cue = conn.execute(
                "SELECT id, character, text FROM pub_line "
                "WHERE revision = ? AND pos < ? ORDER BY pos DESC LIMIT 1",
                (rev, r["pos"]),
            ).fetchone()
            note = conn.execute(
                "SELECT content, updated_at FROM pub_note WHERE revision = ? AND line_id = ?",
                (rev, r["id"]),
            ).fetchone()
            entries.append({"lineId": r["id"], "text": r["text"],
                            "cue": dict(cue) if cue else None,
                            "note": {"content": note["content"], "updatedAt": note["updated_at"]}
                                    if note else None})
        return jsonify({"revision": rev, "label": pub["label"], "character": name,
                        "entries": entries})

    @app.get("/healthz")
    def healthz():
        return "ok"

    # ---- 前端 ---- #
    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/<path:filename>")
    def static_files(filename):
        return send_from_directory(STATIC_DIR, filename)

    return app


app = create_app()

if __name__ == "__main__":
    # 本地开发：python app.py（容器入口会先跑 init_db，再由 gunicorn 启动）
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
