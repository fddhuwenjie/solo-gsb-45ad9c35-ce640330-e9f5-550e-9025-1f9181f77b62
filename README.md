# 小剧场排练 · 剧本换版

一个给小剧场排练用的剧本工作稿 / 发布版管理网页：

- 技术栈：**Python（Flask）+ SQLite + 原生 JavaScript**（无前端框架、无构建步骤、无外部 CDN）。
- 工作稿由有序台词组成，每句台词有 **永不复用的编号**、角色、正文。
- **角色页**：只看本角色的台词，每句带「紧前一句」提示，可给自己的台词写排练笔记。
- **编辑工作稿**：增删、改派角色、改词、上移 / 下移重排，全部暂存后一次保存。
- **提示随顺序即时重算**；**笔记始终跟随原台词编号**，重排、改词都不会丢。
- **删除或改派仍带笔记的台词，整次拒绝**，并明确指出要先到哪个角色页处理笔记——笔记不可能落到别的台词或角色。
- 修改工作稿不影响任何已发布版；**发布**把所有角色页和笔记冻结进同一个修订快照，旧版永远可以打开。
- 编辑台词 / 笔记必须提交「所见工作稿修订号」：过期修改**整次拒绝**，不会覆盖别人（或其他页面）的改动。
- SQLite 数据文件放在 Docker 卷中，**容器重启 / 重建后工作稿和发布版都保留**。

## 一条命令启动

```bash
docker compose up --build -d
```

然后浏览器打开：

> **http://localhost:8000**

停止：`docker compose down`（加 `-v` 会连数据卷一起删除，慎用）。

首次启动会自动写入一段原创的示例小场景（3 个角色、7 句台词、1 条示例笔记），并已冻结发布为 **r1**，方便直接体验「工作稿继续改 → 再发布 r2 → 旧版 r1 仍可打开」。

## 页面说明

| 页面 | 入口 | 能做什么 |
| --- | --- | --- |
| 首页 | `http://localhost:8000/#/` | 工作稿概览、角色入口、发布当前修订、打开各已发布版 |
| 编辑器 | `#/edit` | 增删 / 改派 / 改词 / 重排，一次保存为一个新修订 |
| 角色页 | `#/role/角色名` | 看本角色台词 + 紧前提示，写 / 改 / 清空笔记 |
| 已发布全剧 | `#/pub/修订号` | 只读查看冻结快照（含冻结笔记） |
| 已发布角色页 | `#/pub/修订号/role/角色名` | 只读查看冻结的提示、台词与笔记 |

## 修订号与冲突规则（为什么不会互相覆盖）

- 工作稿有一个单调递增的修订号，任何成功的改动（编辑台词或保存笔记）都会让它 **+1**。
- 保存时必须带上当前页面显示的「所见修订号」。服务端发现不是最新修订：
  - **HTTP 409**：整次拒绝，提示刷新；页面上的未保存改动不会悄悄覆盖别人的改动。
- 对带笔记的台词执行删除或跨角色改派：
  - **HTTP 422**：整次拒绝，响应里列出每条受阻台词编号、所属角色和处理入口；必须先去对应角色页清空或改写笔记。
- 一批编辑操作在**一个数据库事务**内提交，任何一条不合法则整批回滚，绝不出现「删了一半」。
- 笔记以台词编号为主键外键；编号由 SQLite `AUTOINCREMENT` 生成，删除后该编号也不会被新台词复用，因此笔记永远不会错挂。

## 数据持久化与备份

- 数据文件：容器内 `/data/rehearsal.db`（WAL 模式，另有 `-wal` / `-shm` 伴随文件）。
- Compose 中由命名卷 `rehearsal-data` 承载。备份时先停容器，再复制整个数据目录：

```bash
docker compose stop
docker run --rm -v rehearsal-script_rehearsal-data:/data -v "$PWD":/backup \
  alpine tar czf /backup/rehearsal-backup.tgz -C /data .
docker compose start
```

## 不用 Docker 的本地运行

需要 Python 3.11+：

```bash
pip install -r requirements.txt
python app.py
```

访问 **http://localhost:8000**。数据文件默认在 `./data/rehearsal.db`（可用环境变量 `SCRIPT_DB` 改路径，`PORT` 改端口）。
删除该文件后重启会重新生成示例工作稿。

## 目录结构

```
.
├── app.py                  # Flask 应用：API + 修订号/事务/冻结全部逻辑
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── docker-entrypoint.sh    # 启动前单次建表/播种，再起 gunicorn
├── smoke_test.py           # 后端规则端到端冒烟（61 项断言）
├── tests/
│   ├── fuzzgen.js          # 随机编辑场景生成（复刻前端 ops 算法）
│   └── fuzz_run.py         # 用真实后端执行并比对结果（数千场景）
└── static/
    ├── index.html          # 单页外壳（hash 路由）
    ├── app.js              # 原生 JS：首页 / 编辑器 / 角色页 / 发布版
    └── styles.css
```

## 自带测试

```bash
pip install flask            # 仅测试需要
python smoke_test.py         # 61 项：修订号、笔记阻挡、事务回滚、冻结、并发等

# 前后端契约 fuzz：随机生成数千种 增删/改派/改词/重排 组合，
# 用前端同一套 ops 生成算法产出操作，再交给真实后端执行并逐行比对
node tests/fuzzgen.js 3000 > /tmp/fuzz.json
python tests/fuzz_run.py /tmp/fuzz.json
```

## 主要 API（便于调试 / 脚本对接）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/draft` | 工作稿（含每句紧前提示、笔记、修订号） |
| POST | `/api/draft` | 批量增删 / 改派 / 改词 / 重排，body 含 `revision`、`ops[]` |
| GET | `/api/characters/<角色名>` | 角色页（提示 + 笔记） |
| PUT | `/api/lines/<编号>/note` | 保存 / 清空笔记，body 含 `revision` |
| POST | `/api/publications` | 发布当前修订（冻结全剧 + 全部笔记） |
| GET | `/api/publications` | 发布版列表 |
| GET | `/api/publications/<修订号>` | 某一发布版全剧快照 |
| GET | `/api/publications/<修订号>/characters/<角色名>` | 某一发布版的角色页 |

> 本应用不含登录鉴权，设计上假定运行在可信任的本地 / 剧组内网环境。
