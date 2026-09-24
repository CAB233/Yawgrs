# Yawgrs

将不同来源的规则集作为独立包构建，统一发布为 [sing-box 规则集](https://sing-box.sagernet.org/configuration/rule-set/)。每个包通过 `rules/<name>/build.toml` 声明来源和构建阶段。

## 目录结构与构建环境

```text
rules/<name>/build.toml    包的构建配置
templates/<type>.sh       可复用的 Bash 构建模板
scripts/rulebuild.py      来源获取、校验、依赖排序、工作区准备和产物收集
tests/test_rulebuild.py   构建器与模板的自动化测试
```

执行器为每个包创建独立的临时工作区，并提供以下环境变量：

| 变量 | 用途 |
| --- | --- |
| `RULEDIR` | 当前包在仓库中的目录 |
| `SRCDIR` | 下载或复制的来源文件，文件名取自 `rename` 或 source ID |
| `BUILDDIR` | 构建工作目录，各阶段默认在此执行 |
| `PKGDIR` | 最终待发布的文件 |
| `DEPSDIR` | 已声明依赖的产物副本，按 `<依赖包名>/` 存放 |
| `RULE_NAME` | 当前包名 |
| `RULE_SOURCES_JSON` | source ID 到暂存文件绝对路径的 JSON 映射，包含重命名后的路径 |

包按依赖顺序执行，每个包的流程为：

```text
准备工作区 → source → prepare → build → beyond → 收集产物
```

同一 `command` 数组中的命令依次在同一个进程中执行，因此变量和 `cd` 的效果在该阶段内保留。跨阶段通过工作区中的文件传递结果。任一阶段失败都会中止整批构建，全部成功后才更新输出目录。

## 构建配置

以下示例位于 `rules/example/build.toml`：

```toml
name = "example"
description = "示例规则集"
depends = ["another-package"]

[source.rules]
src = "files/rules.json"  # 也可以使用 HTTP(S) 地址
sha256 = "SKIP"          # 或填写文件实际的 64 位 SHA-256

[prepare]
command = ['cp "$SRCDIR/rules" "$BUILDDIR/rules.json"']

[build]
type = "self"
command = [
  'cp "$BUILDDIR/rules.json" "$PKGDIR/example.json"',
  'sing-box rule-set compile -o "$PKGDIR/example.srs" "$PKGDIR/example.json"',
]

[beyond]
command = ['test -s "$PKGDIR/example.srs"']
```

`name` 必须与包目录名一致，`description` 描述包的用途。可选的 `depends` 声明依赖包名；示例中的 `another-package` 应替换为实际存在的包，独立构建的包可以省略该字段。执行器会检查未知依赖和循环依赖。

`prepare` 用于解压、打补丁或预处理输入，`beyond` 用于后处理和检查产物，这两个阶段均可省略。跨包合并时，在 `depends` 中声明相关包，再从 `$DEPSDIR/<包名>/` 读取其产物。

### 来源与校验

使用 `[source.<id>]` 声明一个来源，同一个包可以声明多个来源：

```toml
[source.list]
src = "https://example.com/latest"
rename = "upstream.json"
sha256 = "SKIP"

[source.config]
src = "files/custom.toml"
rename = "config.toml"
sha256 = "SKIP"
```

| 字段 | 要求 | 含义 |
| --- | --- | --- |
| `src` | 必填 | HTTP(S) 下载地址，或相对于当前包目录的本地文件路径 |
| `sha256` | 必填 | 64 位十六进制 SHA-256，或显式填写 `"SKIP"` 跳过比对 |
| `rename` | 可选，默认使用 source ID | 文件在 `SRCDIR` 中的保存名称 |

本地路径的范围限定在当前包目录内，执行器复制来源文件并保留原文件。上面的两个文件分别保存为 `$SRCDIR/upstream.json` 和 `$SRCDIR/config.toml`。

`rename` 接受单个文件名，支持扩展名和空格。空名称、`.`、`..`、路径分隔符以及同一包中的目标文件名冲突会在获取来源前触发错误。

SHA-256 校验针对文件内容。即使使用 `SKIP`，执行器也会记录实际哈希值；`sources.lock.json` 同时记录 source ID 和暂存文件名 `filename`。修改启用校验的本地文件后，可用 `sha256sum <文件路径>` 获取新校验值。

JSON 模板中的 `build.source = "list"` 始终引用 source ID，执行器提供的映射会解析重命名后的路径。自定义命令使用固定路径时，需要写入实际保存的文件名；也可以通过映射读取：

```bash
source_path=$(jq -er '.list' <<< "$RULE_SOURCES_JSON")
cp "$source_path" "$BUILDDIR/input.json"
```

### 自定义命令与模板

`build.type = "self"` 时，必须提供 `build.command` 数组，用于编写自定义构建命令。

其他 `build.type` 值对应 `templates/<type>.sh`，例如 `type = "domi"` 调用 `templates/domi.sh`。这种配置省略 `build.command`，模板接收 `[build]` 中的其他标量参数，环境变量名为 `RULE_PARAM_<大写字段名>`；例如 JSON 模板的 `target` 对应 `RULE_PARAM_TARGET`。模板参数的字段名使用小写字母、数字和下划线，且以字母开头。

缺失模板，或同时配置模板类型和 `command`，会触发校验错误。模板构建与自定义构建使用相同的来源校验、阶段顺序和工作区约定。

### domi 模板

`type = "domi"` 固定读取 `$BUILDDIR/domi.toml`。在 `prepare` 中准备好该配置及其引用的数据文件，模板自动完成以下步骤：

1. 检查 `domi.toml`，在 `BUILDDIR` 中执行 domi-cli。
2. 收集 `BUILDDIR` 根目录下的 `*.json`，保留文件名复制到 `PKGDIR`。
3. 逐个编译 JSON，在 `PKGDIR` 生成同名 `.srs`，最终同时发布 JSON 和 SRS。

配置缺失、生成结果为空、domi-cli 或 SRS 编译失败时，该包构建立即失败。请将 domi 的 JSON 输出设置在 `BUILDDIR` 根目录，并将其他 JSON 输入文件放在 `SRCDIR` 或构建目录的子目录中。

```toml
[prepare]
command = [
  'cp "$SRCDIR/config" "$BUILDDIR/domi.toml"',
  'cp "$SRCDIR/dlc" "$BUILDDIR/dlc.dat"',
]

[build]
type = "domi"
```

示例假定已声明 `[source.config]` 和 `[source.dlc]`，且配置引用 `dlc.dat`。采用 `rename` 时，相应命令应使用实际暂存文件名。

当前 v2ray 包使用 `rename` 将三个来源暂存为 `$SRCDIR/domi.toml`、`$SRCDIR/dlc.dat` 和 `$SRCDIR/geosite.dat`，再通过 `prepare` 复制到 `BUILDDIR`。`rename` 控制来源文件在 `SRCDIR` 中的名称，`prepare` 负责准备模板读取的构建目录。模板按该配置导出 5 个公开规则集，其中 1 个内部依赖条目用于合并。

## JSON 字段映射

`json` 模板通过 `templates/json.sh` 将上游 JSON 中的值映射到一个列表类型的 sing-box 规则字段。`rules/rpglist/build.toml` 的配置示例如下：

```toml
name = "rpglist"
description = "L4D2 RPG 服务器 IP 规则"

[source.list]
src = "https://github.com/yxnan/block-l4d2-rpg-servers/releases/download/latest/rpglist.json"
sha256 = "SKIP"

[build]
type = "json"
source = "list"             # 引用 [source.list]
select = ".data[].raddr"    # jq 表达式，逐个选取上游值
target = "ip_cidr"          # 规则集中的目标字段
transform = '. + "/32"'     # 可选，对每个选取的值进行加工
output = "l4d2-rpglist"     # 可选，产物文件名前缀，默认使用包名
version = 3                # 可选，默认值为 3
```

| `[build]` 字段 | 要求或默认值 | 含义 |
| --- | --- | --- |
| `type` | 必填：`"json"` | 调用 `templates/json.sh`，配置中省略 `command` |
| `source` | 必填 | 已声明的 source ID，自动解析 `rename` 后的文件路径 |
| `select` | 必填 | 针对整个输入文档执行的 jq 表达式，逐个输出值 |
| `target` | 必填 | 列表类型的规则字段，例如 `ip_cidr`、`domain`、`domain_suffix` 或 `port` |
| `transform` | 默认 `"."` | 针对每个选取值执行的 jq 表达式 |
| `output` | 默认使用包名 | 产物的基础文件名，省略扩展名和目录；生成 `<output>.json` 和 `<output>.srs` |
| `version` | 默认 `3` | sing-box 规则集 source 格式版本，填写整数 |

例如输入包含重复地址：

```json
{"data": [{"raddr": "192.0.2.1"}, {"raddr": "192.0.2.1"}]}
```

构建后得到 `rpglist/l4d2-rpglist.json` 和编译后的 `rpglist/l4d2-rpglist.srs`，JSON 内容为：

```json
{"version": 3, "rules": [{"ip_cidr": ["192.0.2.1/32"]}]}
```

示例中的 `/32` 转换适用于 IPv4 地址。上游已经提供 CIDR 时，省略 `transform` 即可保留原值。

对于 `[source.domains]` 提供的 `{"domains": ["example.com", "example.net"]}`，可以这样配置：

```toml
[build]
type = "json"
source = "domains"
select = ".domains[]"
target = "domain_suffix"
```

此时产物基础文件名默认使用包名。顶层数组 `[80, 443]` 可以使用 `select = ".[]"` 和 `target = "port"`。选取数组成员时写 `.domains[]`，让表达式逐个输出字符串或数字。`select` 和 `transform` 均采用 jq 语法。

需要过滤缺失值或空字符串时，可以显式筛选并转为小写：

```toml
select = '.data[] | .domain | select(type == "string" and length > 0)'
transform = "ascii_downcase"
```

模板对结果排序、去重，生成 `{version, rules: [{<target>: [...]}]}` 后编译为 SRS。输入必须包含恰好一个 JSON 文档。结果中出现 `null`、布尔值、对象或数组，结果为空，或 JSON、jq 表达式格式错误时，构建会失败。规则字段名称和内容由 sing-box 编译器进一步校验。

每次 JSON 模板构建支持一个列表类型的目标字段。多字段、逻辑规则，以及上游已经提供完整 sing-box 规则集的场景，可以使用 `self` 构建准备 JSON，再调用 `sing-box rule-set compile`。

## 本地构建与发布

准备 Python 3.11+、Bash、Git、jq、tar、sing-box 和 domi-cli 后运行：

```bash
# 校验所有包的配置
python scripts/rulebuild.py validate

# 构建全部包
python scripts/rulebuild.py build --output dist

# 构建指定包及其依赖
python scripts/rulebuild.py build --package sukka --output dist

# 根据已有锁文件核对来源内容
python scripts/rulebuild.py build --lock dist/sources.lock.json --output dist
```

输出目录包含 `<包名>/...`、产物索引 `index.json` 和来源记录 `sources.lock.json`。`--lock` 会将本次获取的文件与先前锁文件中的哈希比对；`latest` 等可变地址的内容更新后，比对会失败。

CI 将完整产物发布到 `artifacts` 分支，文件直链格式为：

```text
https://raw.githubusercontent.com/<owner>/Yawgrs/artifacts/<package>/<file>
```

AdGuard 包保留原始 `.txt` 和专用 `.srs`；sing-box 对这种专用二进制规则的 JSON 反编译有限制。其他现有包同时提供 `.json` 和 `.srs`。

首次成功发布 `artifacts` 后，CI 会删除旧的 `release`、`release2` 和 `release3` 分支，旧文件直链随之失效。

## 构建日志

终端中，级别前缀使用颜色区分：`[DEBUG]` 青色、`[INFO]` 绿色、`[WARNING]` 黄色、`[ERROR]` 红色，正文保持默认颜色。日志写入标准错误流，重定向后自动使用纯文本；设置 `NO_COLOR=1` 或 `TERM=dumb` 也会关闭颜色。

日志使用 `[INFO]`、`[DEBUG]`、`[WARNING]`、`[ERROR]` 前缀。默认显示来源获取、构建阶段、产物收集和错误；追加 `--debug` 后显示工作区路径、校验和以及跳过的阶段：

```bash
python scripts/rulebuild.py build --package v2ray --debug
```

模板和外部命令的输出会实时加上日志级别与包/阶段上下文，已有级别会保留或规范化。阶段失败时，错误信息包含包名、阶段和退出码。例如：

```text
[INFO] Building v2ray
[INFO] v2ray: prepare
[INFO] v2ray: template domi
[ERROR] v2ray: template domi: domi: missing .../build/domi.toml; prepare this file in [prepare]
[ERROR] v2ray: template domi: failed (exit code 1)
```

## 自动化测试

`tests/test_rulebuild.py` 用于检查构建器和模板的行为，防止修改代码后破坏已有功能。当前覆盖：

- 来源处理：本地文件与模拟下载的重命名、目标文件名冲突和路径检查、SHA-256 失败时提前终止。
- 构建流程：包工作区隔离、依赖产物传递、`prepare → build → beyond` 阶段执行、配置校验和循环依赖检查。
- JSON 模板：IP、域名和端口映射、排序去重、重命名后的源文件读取、实际 SRS 编译，以及异常提取结果的失败处理。

测试使用临时目录和小型样例，远程下载通过模拟响应验证。执行命令：

```bash
python -m unittest discover -s tests -v
```

实际编译测试需要本机安装 `jq` 和 `sing-box`；缺少相应工具时，这些用例会标记为跳过。CI 的校验任务也会运行这组测试；真实上游内容由后续构建任务验证。修改构建器或模板后，运行测试即可检查这些行为是否仍然符合预期。
