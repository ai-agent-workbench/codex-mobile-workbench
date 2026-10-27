# 第三方声明与来源

项目 MIT 只适用于本项目有权授权的自有部分。以下上游组件的许可／版权／NOTICE 独立保留，不更换为 MIT。此声明不是对未知历史来源的法律担保。

## Codex 协议摘录、生成 schema 与安装来源

`engine/fixtures/codex_app_server_protocol_*.json` 是不同原生版本的协议适配摘要／测试材料；`engine/fixtures/native_interactions_0_159_2/` 的8个 schema 和清单来自 App Server schema dump。它们不是本项目独立创造的全部内容，也不是保证这些旧版本都受支持。按上游 OpenAI Codex 的 Apache-2.0 来源保留归属，并在此记录摘录／整理与用途；JSON 内容保持相应版本的协议事实，不能把 MIT 标签覆盖到这些材料。

2026-10-26 从官方 `openai/codex` 对应版本标签读取 LICENSE 和 NOTICE，核对 Git blob；同时读取 `@openai/codex` npm 对应版本元数据（均声明 Apache-2.0）并验证下载包的 npm SHA-512 integrity。具体来源和刷新时间见 `third_party/codex/upstream-sources.json`；逐文件对应、用途、哈希与摘录／变更说明见 `third_party/codex/fixture-provenance.json`。npm CLI wrapper 包本身未附 LICENSE／NOTICE，因此此候选补充了对应官方仓库标签的两份原文，而不是声称 NOTICE 不存在。

所涉精确版本：0.151.0-alpha.7.2、0.152.1、0.153.1、0.154.0、0.156.0、0.159.2、0.160.0；原文位于 `third_party/codex/`。早期0.151协议摘要实际记录的是 **0.151.0-alpha.7.2，不是0.151.0正式版**。已由2026-09-01原适配提交与当时验收记录补足来源：桌面提供的WSL App Server生成／核对稳定及实验schema哈希；本轮另核实精确alpha版本的npm归档完整性与官方GitHub标签LICENSE／NOTICE。原二进制／完整schema转储未在本候选分发，也未在本轮重新生成；不把相邻正式版的许可文件当作精确版本取证。历史人工复制源码／模板／素材的总体确认仍单独保留，不因技术来源补证而作法律担保。

安装器使用固定官方 Codex 0.160.0 Linux x86-64 musl **完整运行包**，归档及全部成员的固定大小／校验值见 `shared/instance_native.py` 和[原生运行包来源记录](third_party/codex/runtime-package-0.160.0.json)。按官方布局保留 CLI、code-mode host、辅助工具、可选资源及其 NOTICE／许可证（含可选语音资源的 LGPL 等独立许可证）；我们的 MIT 声明不改变这些上游组件的许可，保留资源不等于启用该功能。本源码候选**不携带这些原生二进制或共享库**，安装时由用户从官方获得。若未来把原生二进制、离线wheels或完整runtime一起分发，应重新复核该实际成品内全部组件及其NOTICE／源码义务，不能沿用本声明假称已审完。

## Python依赖

本候选不携带site-packages或wheels。新实例在线安装 `requirements-core.lock` 的33个固定版本；其他lock为保留的可选／开发路径，不是全部功能的公开支持承诺。下面根据本机**相同精确版本**的上游dist-info元数据与法律文件生成，50个锁定组件全部版本匹配，共保留71份法律文本。元数据不是独立来源认证；泛称BSD或分类器信息不擅自改写成更精确的SPDX。

完整机读清单及文件哈希见 `third_party/python-dependencies.json`；对应LICENSE／COPYING／NOTICE原文位于 `third_party/python/`。保留这些法律文本不表示已把库代码随包分发。

| 组件 | 版本 | 许可元数据摘要 | 用途 |
|---|---|---|---|
| aiohappyeyeballs | 2.7.1 | PSF-2.0 | 可选／开发 |
| aiohttp | 3.14.3 | Apache-2.0 AND MIT | 可选／开发 |
| aiosignal | 1.4.0 | Apache 2.0 | 可选／开发 |
| annotated-doc | 0.0.4 | MIT | 核心 |
| annotated-types | 0.7.0 | OSI Approved :: MIT License | 核心 |
| anyio | 4.14.1 | MIT | 核心 |
| attrs | 26.1.0 | MIT | 核心 |
| cbor2 | 6.1.4 | MIT | 核心 |
| certifi | 2026.7.22 | MPL-2.0 | 可选／开发 |
| cffi | 2.1.1 | MIT-0 | 核心 |
| charset-normalizer | 3.5.1 | MIT | 可选／开发 |
| click | 8.4.2 | BSD-3-Clause | 核心 |
| cryptography | 50.0.0 | Apache-2.0 OR BSD-3-Clause | 核心 |
| fastapi | 0.139.0 | MIT | 核心 |
| frozenlist | 1.8.0 | Apache-2.0 | 可选／开发 |
| h11 | 0.16.0 | MIT | 核心 |
| http-ece | 1.2.1 | MIT | 可选／开发 |
| idna | 3.18 | BSD-3-Clause | 核心 |
| itsdangerous | 2.2.0 | OSI Approved :: BSD License | 核心 |
| jinja2 | 3.1.6 | OSI Approved :: BSD License | 核心 |
| jsonschema | 4.25.1 | MIT | 核心 |
| jsonschema-specifications | 2025.4.1 | MIT | 核心 |
| markupsafe | 3.0.3 | BSD-3-Clause | 核心 |
| multidict | 6.7.1 | Apache License 2.0 | 可选／开发 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | 可选／开发 |
| propcache | 0.5.2 | Apache-2.0 | 可选／开发 |
| py-vapid | 1.9.4 | MPL-2.0 | 可选／开发 |
| pyasn1 | 0.6.4 | BSD-2-Clause | 核心 |
| pyasn1-modules | 0.4.2 | BSD | 核心 |
| pycparser | 3.0 | BSD-3-Clause | 核心 |
| pydantic | 2.13.4 | MIT | 核心 |
| pydantic-core | 2.46.4 | MIT | 核心 |
| pyopenssl | 26.4.0 | Apache License, Version 2.0 | 核心 |
| pyotp | 2.10.0 | MIT | 核心 |
| python-multipart | 0.0.32 | Apache-2.0 | 核心 |
| pywebpush | 2.5.0 | MPL-2.0 | 可选／开发 |
| qrcode | 8.2 | BSD | 核心 |
| referencing | 0.36.2 | MIT | 核心 |
| requests | 2.34.2 | Apache-2.0 | 可选／开发 |
| rpds-py | 0.27.1 | MIT | 核心 |
| setuptools | 84.0.0 | MIT | 可选／开发 |
| starlette | 1.3.1 | BSD-3-Clause | 核心 |
| typing-extensions | 4.16.0 | PSF-2.0 | 核心 |
| typing-inspection | 0.4.2 | MIT | 核心 |
| urllib3 | 2.7.0 | MIT | 可选／开发 |
| uvicorn | 0.51.0 | BSD-3-Clause | 核心 |
| webauthn | 3.0.0 | BSD-3-Clause | 核心 |
| websockets | 16.1 | BSD-3-Clause | 核心 |
| wheel | 0.48.0 | MIT | 可选／开发 |
| yarl | 1.24.5 | Apache-2.0 | 可选／开发 |

MPL-2.0组件（例如可选路径的certifi／py-vapid／pywebpush）不因为主项目MIT而失去其文件级许可义务。实际再分发这些组件或修改其覆盖文件时，须另行遵守其源码、许可和变更要求。cryptography 等多许可组件按其上游文本处理。

## 图标、界面与框架

候选使用新编写的纯几何向量源 `static/workbench-icon.svg`，MIT，2026 ai-agent-workbench contributors；PNG由 `scripts/build-icons.sh` 栅格化。未携带缺少授权记录的旧PNG。没有从其他项目抽取logo／字体或使用OpenAI商标来冒充官方产品。

README 的 `docs/assets/workflow-demo.png` 来自本项目合成页面，不是真实手机截图或第三方素材；页面与生成步骤见 [演示图片来源](docs/assets/README.md)。

Windows面板为本项目C#源码编译，不随包再分发.NET Framework。运行系统框架仍适用微软自己的条款；未签名程序的可信来源与系统策略另行核对。项目名称中对Codex的提及仅用于说明集成关系。

## 来源核对范围

已对拟公开源码树、现存归属材料、协议材料及图标生成来源进行检查。当前树未发现 HAPI／外部 Bridge 项目直接引入、submodule 或另有未登记复制素材的具体证据；这不是证明未知历史人工复制从未发生，也不是法律担保。

如后续发现遗漏的复制源码、模板或素材，应核对来源并补充对应许可／归属，必要时移除或替换；不能以“AI 写的”替代检查，也不能用新建仓库来抹去第三方权利。涉及敏感信息时使用 [私密安全报告](SECURITY.md)，不要公开上传私人资料。
