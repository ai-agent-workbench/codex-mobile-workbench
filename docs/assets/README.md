# 演示图片来源

`workflow-demo.png` 是本项目 `docs/demo.html` 的合成交互流程截图，自有部分采用项目 MIT 许可。未使用真实账号、网络入口、业务资料、私人手机截图、第三方 logo 或字体文件。

生成步骤：

1. 用 Chromium 打开本地 `docs/demo.html`，视口设为 390×1000、deviceScaleFactor 为 1。
2. 点击 `#attach`，再点击 `#run`，等待 `#result` 显示。
3. 截取 `.phone` 元素；截图展示的是公开样例，不是真实模型执行或实机录像。

此次使用 Chromium 151.0.7922.34。页面无需网络；模拟 CSV 下载、模拟返回不重复执行及重置行为已单独检查。不同浏览器或系统字体可能产生不同渲染像素。
