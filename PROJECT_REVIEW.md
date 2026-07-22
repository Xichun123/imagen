# Imagen 项目审查与 TODO

## 审查范围

本次审查覆盖：

- `SKILL.md`
- `scripts/imagen.py`
- `tests/test_imagen.py`
- `references/*.md`
- 供应商与模型配置
- generate、edit、generate-batch 行为
- 输出安全、错误处理、API 兼容性、依赖和项目维护性

审查期间未进行真实 API 请求。当前 8 个标准库测试均通过，但当前环境未安装 `openai` 包，因此实时 API 路径尚未验证。

## 总体结论

当前项目已经具备可用的多供应商结构：

- 支持自定义供应商 ID
- 支持 `default_provider`
- 每个供应商支持真实模型 ID 列表
- 支持 `default_model`
- 支持运行时通过 `--provider` 和 `--model` 切换

主要风险集中在批处理预检、输出文件安全、供应商兼容性和测试覆盖。若用于长期自动化，建议先完成 P0 项目。

---

## 已确认的问题

### 1. 批处理没有在网络调用前完成全量预检

**优先级：P0**

`generate-batch` 在每个异步任务内部验证模型、参数和输出路径。一个错误 job 可能在其他 job 已经开始调用 API 后才被发现，导致：

- 部分任务已经产生费用
- 批次只完成一部分
- 错误处理结果不一致
- 无法保证 `--fail-fast` 真正发生在首次网络请求前

此外，内部 `_die()` 抛出 `SystemExit`，而 batch 主要捕获 `Exception`。`SystemExit` 不属于 `Exception`，可能绕过按 job 记录失败和任务取消逻辑。

相关位置：

- `scripts/imagen.py:41-44`
- `scripts/imagen.py:686-719`
- `scripts/imagen.py:741-756`

### 2. 输出写入不是全量预检和原子操作

**优先级：P0**

当前实现逐个检查、逐个写入输出。如果后面的路径冲突，前面的文件可能已经写入。

主要风险：

- `n > 1` 时可能部分成功
- batch 中重复 `out` 可能发生竞争
- `--force` 下重复路径可能并发覆盖
- API 返回图片数量少于请求数量时会静默少写
- 中断时可能留下不完整文件
- 原图和 downscale 路径可能相同

已确认：当使用空的 `--downscale-suffix ""` 时，原图和缩略图路径相同。

相关位置：

- `scripts/imagen.py:407-437`
- `scripts/imagen.py:519-549`
- `scripts/imagen.py:711-739`
- `scripts/imagen.py:815-823`

### 3. 通用供应商能力仍受固定 API 协议限制

**优先级：P1**

当前实现依赖：

- `openai` Python SDK
- `images.generate(...)` / `images.edit(...)`
- `/v1/images/generations` / `/v1/images/edits` 语义
- `data[].b64_json` 响应
- 固定的 size、quality、background、output_format 参数集合

因此，当前支持的是实现这套兼容协议的供应商，并不能仅通过 URL 接入任意不同协议的生图 API。

另外，脚本默认发送 `size=1024x1024`、`quality=auto`、`output_format=png`。部分兼容站点可能不支持这些默认参数。

相关位置：

- `scripts/imagen.py:215-260`
- `scripts/imagen.py:440-445`
- `scripts/imagen.py:619-628`
- `references/image-api.md:12-38`

### 4. 只验证当前供应商，不验证备用供应商

**优先级：P1**

当前 `_configure_provider()` 只验证本次选中的供应商。

已确认：备用供应商即使 URL、`api_key_env` 和模型配置错误，只要默认供应商正确，默认供应商的 dry-run 仍会通过。

这意味着备用供应商可能直到真正切换时才暴露错误，不符合“提前配置好即可随时切换”的目标。

相关位置：

- `scripts/imagen.py:115-177`

### 5. 参数验证与文档存在偏差

**优先级：P1**

已确认的问题：

- PNG 输出仍接受 `--output-compression`
- 文档说输入图片和 mask 必须小于 50MB，实现仅警告并继续
- 输入路径只检查存在，不检查是否为普通文件
- `--out` 和 `--out-dir` 同时提供时，`--out` 被静默忽略
- dry-run 会创建输出目录
- `.jpg` 与 `output_format=jpeg` 可能产生不必要的扩展名警告

相关位置：

- `scripts/imagen.py:194-212`
- `scripts/imagen.py:247-261`
- `scripts/imagen.py:264-287`
- `scripts/imagen.py:519-539`
- `references/image-api.md:20-25,40-46`

### 6. retry-after 小数解析错误

**优先级：P1**

重试正则中小数点被写成了 `\\.`。实测：

```text
retry-after: 2.5 -> 2.0
```

这会低估服务端要求的等待时间。

另外，batch 自己实现重试，而 SDK 也可能自带重试；如果不明确关闭其中一层，实际请求次数和等待时间可能叠加。

相关位置：

- `scripts/imagen.py:552-565`
- `scripts/imagen.py:586-610`

### 7. 测试覆盖主要集中在配置解析

**优先级：P1**

当前 8 个测试覆盖了：

- 默认供应商
- 显式供应商切换
- 模型选择
- 未知供应商和模型
- 配置路径环境变量
- 缺失密钥
- edit/batch dry-run 的供应商选择

尚未覆盖：

- 模拟 API 成功响应
- base64 解码和文件写入
- API 返回数量不匹配
- 输出冲突和原子写入
- 429、timeout、retry-after
- `--fail-fast`
- batch 并发行为
- downscale
- mask 和多图 edit
- 无效 JSON、无效 URL、重复模型、错误默认引用
- 单供应商和单模型自动选择
- `--config` 相对 `IMAGEN_CONFIG_PATH` 的优先级

### 8. 依赖没有项目级声明和版本约束

**优先级：P1**

项目没有 `pyproject.toml`、lock 文件或 requirements 文件。文档只提供：

```bash
uv pip install openai
```

风险包括：

- SDK 参数签名随版本变化
- `AsyncOpenAI` 可用性不确定
- 默认重试和 timeout 行为不确定
- 不同环境无法稳定复现

当前环境状态：

- `openai`：未安装
- Pillow：已安装，版本 12.2.0

### 9. Skill 主文件和 references 重复较多

**优先级：P2**

`SKILL.md` 当前约 272 行，与以下文件存在重复：

- `references/cli.md`
- `references/prompting.md`
- `references/sample-prompts.md`

供应商配置示例同时存在于 `SKILL.md` 和 `references/cli.md`，后续修改容易遗漏同步。

建议让 `SKILL.md` 只保留触发条件、核心决策、执行规则和安全边界，把完整配置、taxonomy 和示例放入 references。

### 10. 路径文档偏向 Codex，不完全适配其他 Agent

**优先级：P2**

`references/cli.md` 假设 Skill 位于：

```text
$CODEX_HOME/skills/imagen
```

当前 Skill 也面向 Pi 和其他 Agent，固定使用 `CODEX_HOME` 会降低可移植性。建议优先使用 Skill 目录的相对路径，或同时说明不同安装位置。

### 11. 部分 raster/vector 边界表述冲突

**优先级：P2**

Skill 明确只输出 raster，但 logo 示例多次写成：

```text
vector logo mark
vector wordmark
```

这容易让 Agent 误以为最终会得到真实 SVG/vector 文件。建议改为：

```text
vector-like flat graphic rendered as raster
```

或者将需要真实矢量文件的 logo 请求明确排除。

### 12. 项目维护基础设施不足

**优先级：P2**

当前目录不是 Git 仓库，并且没有发现：

- `.gitignore`
- `pyproject.toml`
- 自动格式化/静态检查配置
- CI
- JSON Schema 或 provider 配置示例文件

此外，`scripts/imagen.py` 中存在未使用的 `_decode_and_write()`。

---

## TODO List

### P0：优先修复

- [x] 将 batch job 的模型、参数、格式和输出路径验证移到创建异步任务之前
- [x] 在任何 API 请求前完成整个 batch 的预检
- [x] 避免在内部业务逻辑中使用 `SystemExit` 作为可恢复错误
- [x] 为 batch 建立统一的 job 错误类型和退出码
- [x] 预先检测所有输出路径重复，包括不同 job 的重复 `out`
- [x] 禁止原图路径与 downscale 路径相同
- [x] 写入前一次性检查全部目标文件是否存在
- [x] 使用临时文件加原子替换写入最终输出
- [x] 校验 API 返回图片数量与期望输出数量一致
- [x] 明确 `--force` 在 batch 并发场景中的行为（允许覆盖预存文件，但始终禁止批次内重复目标）

### P1：可靠性和供应商能力

- [x] 新增 `config-check` 子命令，验证所有供应商而非只验证当前供应商
- [x] 新增 `providers` 或 `list-providers` 子命令，列出默认供应商、模型和密钥状态
- [x] 校验 `default_provider` 必须存在于 `providers`
- [x] 校验每个 `default_model` 必须存在于对应 `models`
- [x] 为 provider 配置增加可选 timeout
- [x] 明确并统一 SDK 重试与 CLI 重试，避免双层重试
- [x] 修复 retry-after 小数正则
- [x] 评估并实现 provider 级 `defaults`、`supported_params` 和 `extra_headers`
- [x] 对不同协议的供应商设计 adapter 接口，而不是只替换 URL
- [x] 仅发送用户或 provider 显式启用的可选 API 参数
- [x] dry-run 不创建目录或写入任何文件
- [x] `output_compression` 仅允许 JPEG/WebP
- [x] 输入图片和 mask 超过限制时统一为报错
- [x] 输入图片、mask、prompt file、JSONL 必须验证为普通可读文件
- [x] 将 `--out` 与 `--out-dir` 设为互斥参数
- [x] 正确处理 `.jpg` 与 `jpeg` 的等价关系
- [x] 对 provider URL 做安全输出处理，避免打印可能包含凭据的 query/userinfo

### P1：测试和依赖

- [x] 增加 fake OpenAI-compatible client，测试成功和失败响应
- [x] 测试 base64 解码、无效 base64 和返回数量不匹配
- [x] 测试单图、多图和 `n > 1` 输出
- [x] 测试输出冲突、重复 batch out 和 `--force`
- [x] 测试 429、timeout、retry-after 和最大重试次数
- [x] 测试 `--fail-fast` 是否取消未开始任务
- [x] 测试 downscale 及空 suffix
- [x] 测试 mask、多图 edit 和文件关闭
- [x] 测试损坏 JSON、无效 URL、无效环境变量名和重复模型
- [x] 测试单供应商/单模型自动选择
- [x] 测试 `--config` > `IMAGEN_CONFIG_PATH` > 默认路径的完整优先级
- [x] 新增 `pyproject.toml`
- [x] 约束并验证 `openai==2.47.0` SDK 版本
- [x] 将 Pillow 声明为 optional dependency
- [x] 增加统一测试命令和 CI

### P2：文档和维护

- [x] 缩短 `SKILL.md`，将完整供应商配置移到单一 reference
- [x] 避免 `SKILL.md` 与 `references/cli.md` 重复维护配置示例
- [x] 增加 `providers.example.json`
- [x] 增加 provider 配置 JSON Schema
- [x] 将 Codex 专属路径说明改为跨 Agent 安装说明
- [x] 修正 logo 示例中的真实 vector/raster 边界
- [x] 删除未使用的 `_decode_and_write()`
- [x] 增加 `.gitignore`，排除 `__pycache__/`、`tmp/`、`output/` 和本地配置
- [x] 初始化 Git 仓库并建立可回滚的提交历史
- [x] 在 Skill frontmatter 中补充 Apache-2.0 license 字段

---

## 推荐实施顺序

1. Batch 全量预检
2. 输出路径和原子写入安全
3. `config-check` 与供应商列表命令
4. provider 级参数、timeout 和能力配置
5. 参数验证与 retry 修复
6. fake client 测试与依赖固定
7. Skill 文档精简和项目基础设施

## 明确保留的设计决策

以下行为建议继续保留：

- 默认不自动跨供应商重试，避免重复计费
- 使用 `--provider` 显式切换备用供应商
- API key 保存在环境变量或 secret manager 中，不写入 provider JSON
- `--model` 直接使用供应商真实模型 ID
