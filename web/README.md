# ResearchAgent UI Prototype

Gemini 提供的 React 18 + TypeScript + Vite + Tailwind CSS 3 + Lucide React + Zustand 前端原型，按原目录整理。无需 Python、FastAPI、模型密钥或 MCP 服务。

## 运行

建议使用 Node.js 22 LTS（也可使用兼容 Vite 6 的其他 Node 版本）。在项目根目录运行：

```bash
npm install
npm run dev
```

在 Windows 浏览器打开 http://localhost:5173 （实际地址以终端输出为准）。按 Ctrl+C 停止。首次安装需要访问 npm registry。可执行 `npm run build` 检查 TypeScript 并构建静态资源。

## 体验

- 默认示例会话：展开工具卡片，查看 Diff，批准或拒绝模拟审批。
- 轨迹：切换时长/轮次/调用按钮，点击色块查看事件明细，搜索事件。原版轮次与调用采用相同等宽展示，非真实时序或并发测量。
- 新建任务：输入消息后模拟回复，运行中可停止；双击工作区可在对应目录新建任务。
- Skills / MCP：模拟启停；检查器显示会话信息。

## 原型边界与调整

所有数据、命令结果、权限、MCP 和打开文件夹行为均为 Mock；不会实际执行命令或访问项目目录。刷新页面会恢复初始数据。对话文本保留原版纯文本展示，没有新增 Markdown 渲染库。保留原 UI 风格和主要组件，对源码进行了紧凑整理，并调整 Vite 不自动打开浏览器（适配 WSL）、每会话独立模拟计时器、审批幂等、等待审批时防重复发送、中文输入法回车处理、会话切换模型同步、复制失败反馈、泳道定位和工作区双击新建任务。打开文件夹提示明确标注 Mock。

验证：已检查 TypeScript/TSX 语法并使用测试替身运行部分状态管理逻辑。打包环境无法解析 registry.npmjs.org，未完成真实依赖安装、生产构建和浏览器交互测试。
