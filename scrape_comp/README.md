# scrape_comp — 单个阵容页抓取 / 棋盘解密 / 分析文档生成

`scrape_comp` 负责把 **某个具体阵容页** 变成一份能照着打的 Markdown 文档:
运营思路、搜牌节奏、所需装备(必做/破抗/重伤/主C/主T/副C/副T)、主C主T定位、前排/后排羁绊、
每个棋子在第几行第几列。

与 [`scrape_info`](../scrape_info) 的分工: `scrape_info` 抓全站基础库(棋子/装备/羁绊/阵容榜),
`scrape_comp` 抓单个阵容并解出棋盘。

## 用法

```bash
# 在项目根目录, 模块模式运行
python -m scrape_comp "https://jcc.datatft.com/comp/rank#16278-33039-凯特琳-主宰"
python -m scrape_comp 16278                       # 也可以只给阵容 id
python -m scrape_comp 16278 --season 17           # 换赛季
python -m scrape_comp 16278 --refresh-data        # 重新下载静态数据(默认走缓存)
python -m scrape_comp 16278 --json                # 只打印分析 JSON, 不写文件
python -m scrape_comp 16278 --out D:\tmp\comps    # 换输出根目录
```

输出(默认 `./workspace/scrape_comp/<id>-<纹章id>-<标题>/`):

| 文件 | 内容 |
|:---|:---|
| `raw.json` | 三个接口的原始响应(strategy / extra / rank), 便于排查 |
| `analysis.json` | 结构化分析结果(文档就是它的渲染) |
| `阵容分析.md` | 人读文档 |
| `../_cache/game_data_cn.json` | 静态数据缓存(约 2MB), 删掉即重新下载 |

## 抓取链(2026-10 实测)

```
https://jcc.datatft.com/comp/rank#16278-33039-凯特琳-主宰
  → 外层只是 iframe 壳: /service/tool/intent-wheel.html#/comps/a/<slug>
  → 数据全在 api.datatft.com:
       POST /team/strategy        {id}           攻略正文 + 推荐出装 + teamUrl + 阵容码
       POST /team/strategy/extra  {id}           实战装备统计(按 unitKey 分组的成型三件套)
       POST /team/rank            {season}       评级 / 主C(heroId) / 三星 / 成装
       POST /team/list            {version,...}  榜单(需要游戏版本号, 本模块不用)
  → 棋盘: teamUrl(密文) 用 codec.py 解密
  → 中文名: static.datatft.com/tft-base/languages/cn.<hash>.json (base64 + AES-128-ECB)
```

`#` 后面的 `<阵容id>-<纹章id>-<主C名>-<羁绊名>` 只有前两段有用 (站点前端的
`Pi()` 用 `^(\d+)(?:-(\w+))?` 取 id 与 equipId)。

## teamUrl 密文格式(逆向前端 intent-wheel 得到)

```
DBI3BwBtCCJAqKAiLCIOAuVBEXAKZAF3BcBjBYbAR3BeBVBV|KFLvKtLoJNGkHoHvFUs18
└──────────────── 棋子段 ────────────────┘ └─ 海克斯段 ─┘ └ 赛季后缀 s18
```

* 码表不是固定字母表, 而是**按静态数据的顺序现场生成**的:
  * 位置码 `map0..map55` ← `"ABC…abc…!<>."`(56 个字符), 56 格 = 8 行 × 7 列,
    站点只取 **前 28 格**当我方棋盘;
  * 英雄码 ← 英雄自带的 `code`, 没有 code 的(野怪/召唤物假棋子)**按顺序发 `z`+字母**兜底;
  * 装备码 ← 装备列表**剔掉 `612/545/573/31004~31007/2252` 后, 把 `31004~31007` 追加到末尾**,
    再按 `字母[行] + 字母[列]` 两位生成;
  * 海克斯码 ← `hexs18` 三组词条自带的 `code`。
  → 所以 `codec.py` 必须用**站点同源**的静态数据建表, 换数据源会整体解错。
* 棋子段单条记录: `位置(1) + 英雄(2) [+ 装备件数(1) + 装备码(2×N)] [+ "~" 强化果实]
  [+ "*" 星级(1)] [+ "_" 变形棋子(2)] [+ "^" 额外羁绊(1×N)] [+ "-" 主C标记(2)]`
* 海克斯段在 `|` 之后, 每 2 字符一个, 直到字符串结束。

棋盘方位: 数组下标 `index = (行-1)*7 + (列-1)`, **第 1 行是最靠近敌方的前排**,
第 4 行是我方底线 (实测: 坦克在第 1~2 行, 远程 C 位在第 4 行)。

## 模块结构

| 文件 | 职责 |
|:---|:---|
| `config.py` | 站点/接口/码表常量/输出路径/功能装备关键词 |
| `gamedata.py` | 静态数据 URL 发现 + 下载 + AES 解密 + 查询封装 |
| `codec.py` | 复刻站点编解码表, `Codec.decode(team_url)` + 棋盘坐标换算 |
| `api.py` | `DatatftClient`: strategy / strategy_extra / team_rank / team_list |
| `analyze.py` | 结构化分析: 角色打分、装备三来源交叉、羁绊计数、搜牌节奏抽取 |
| `report.py` | 把分析结果渲染成 Markdown |
| `scraper.py` | 编排与落盘, `parse_comp_ref` 解析网址 |
| `__main__.py` | CLI |

## 关键判定口径(文档里也会写明)

* **必须装备**: 主C/主T 的装备里, 「棋盘快照」「榜单阵容」「攻略推荐」**三个来源都出现**的;
  另外攻略正文写「必做/最关键/优先抢」的装备会被单独点名。
* **主C**: 站点榜单的 `heroId`(权威); 取不到时按 后排 + 装备数 + 星级 + 费用 + 攻击距离打分。
* **主T**: 前排里 装备数 + 星级 + 费用 + 血量 最高者; 副C/副T 依此顺延。
* **破抗/重伤**: 按装备 `effect` 里的「护甲击碎 / 魔抗击碎 / 重伤」关键词分类,
  分「阵容已覆盖」与「备选可合成」两栏; 另按主C/副C 的英雄定位**推断**伤害类型,
  给出该优先破甲还是破破魔抗的建议(文档里标明是推断)。
* **前排羁绊 / 后排羁绊**: 按贡献棋子所在行判定 (全部在前排=前排羁绊, 全部在后排=后排羁绊,
  两边都有=前后排混合)。纹章带来的额外计数单列一节(`/team/rank` 里能看到棋子带纹章)。

## 已知局限

* 站位是站点记录的快照, 游戏内视角可能左右镜像; 列号按地图从左到右 1~7。
* 密文里没编码星级时, 星级取自 `/team/rank`(榜单阵容), 可能与攻略作者当时的星级不同。
* 攻略文本写「4主宰+3猎人」这类数字有时与实测棋盘不一致, 文档会在「提示」里指出差异。
* 强化符文段是站点跟着 `teamUrl` 一起存的, 只作参考, 不代表一定能选到。
* 站点没有中文名的 id(如 18 赛季 `999001~999004`)在文档里标成 `未命名(99900X)`, 不猜名字。

## 测试

```bash
# 离线冒烟: 只验参数解析, 不联网
python -m scrape_comp --help

# 联网抓一个真实阵容, 只打印分析 JSON (不落文件)
python -m scrape_comp 16278 --json
```
