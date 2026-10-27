# 手机工作台合成基准（按需运行）

这些工具不进入默认回归，不连接生产、App Server、模型或通知。
用同一机器、依赖、数据与采样次数比较候选；JSON 由调用方保存，不提交临时数据库。

```bash
# 真实 Store，20/200/1000 轮、127/1270/6351 items；1 次预热、7 次记录。
.venv/bin/python tests/benchmarks/conversation_reads.py --reader snapshot
.venv/bin/python tests/benchmarks/conversation_reads.py --reader summary

# 真实文件 route，505/5000 个临时文件；扫描和 HTML 分开计量。
.venv/bin/python tests/benchmarks/file_listing.py "$PWD"

# 真实模板/静态脚本，20/200/500 轮，每轮固定约 1 KiB 文本。
# 使用本机既有 Playwright 环境；8 次运行包括 1 次预热。
NODE_PATH="$HOME/Tools/node_modules" PLAYWRIGHT_BROWSERS_PATH="$HOME/Tools/browsers/playwright" \
  node tests/benchmarks/conversation_render.js --source-dir "$PWD" --runs 8
```

读取与渲染工具支持 `--source-dir /path/to/baseline`，文件工具第一个位置参数为源码目录。
基准目录自身的 Python/Node 依赖须可用，不会安装依赖或改服务。

渲染数据使用完整兼容快照，刻意不靠只加载 20 轮降低 500 轮测试规模。
分别记录首个历史 DOM、全部 Markdown 完成、单条尾部更新，并检查首节点身份和尾文确实更新。
它只证明本机浏览器渲染开销，不代表端到端网络、实体手机或分段 API 载荷。
文件页仍需枚举同目录以正确过滤和排序，HTML 变小不等于消除全目录扫描。

七样本的文件/浏览器 p95 使用 nearest-rank（最大样本）；Store 延续 S0 的线性插值口径。
只比较同一工具/口径的前后值，不以一次最好样本宣称改进。
