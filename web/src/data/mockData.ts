import { McpServerItem, Session, SkillItem, Workspace } from '../types/agent';
// Illustrative context capacity shared by the prototype models.
export const MOCK_CONTEXT_LIMIT = 1_000_000;
export const AVAILABLE_MODELS = [
  {id:'DeepSeek-V4-Pro High',protocol:'openai' as const}, {id:'Claude-3.7-Sonnet',protocol:'anthropic' as const}, {id:'GPT-5-Codex',protocol:'openai' as const},
];
export const INITIAL_WORKSPACES: Workspace[] = [
  {id:'ws-research',name:'ResearchAgent',path:'~/projects/ResearchAgent'},
  {id:'ws-shopsage',name:'ShopSage-RAG',path:'~/projects/ShopSage'},
  {id:'ws-homepage',name:'Personal_Homepage',path:'~/projects/Personal_Homepage'},
  {id:'ws-tmp',name:'tmp',path:'~/projects/tmp'},
  {id:'ws-default',name:'默认工作区',path:'~',isDefault:true},
  {id:'ws-ungrouped',name:'未分组',path:'~',isUngrouped:true},
];
export const INITIAL_SESSIONS: Session[] = [
  {id:'sess-006-isolation',workspaceId:'ws-research',title:'排查多项目缓存与非阻塞 Shell 隔离',modeLabel:'标准模式',updatedAt:'刚刚',protocol:'openai',model:'DeepSeek-V4-Pro High',status:'waiting_approval',metrics:{rounds:2,steps:7,tokensPerSec:42,contextTokens:{system:1500,tools:5600,messages:58300},cacheHitRate:83,foldedCount:1},messages:[
    {id:'m1',role:'user',content:'帮我检查 agents/tools.py 里的权限缓存和 Shell 执行，看看单进程多项目并发时有没有阻塞和串台问题。',timestamp:'11月27日 14:12'},
    {id:'m2',role:'assistant',content:'已定位到两处核心问题：\n1. `_cached_rules` 和 `_activated_tools` 仍为模块级全局变量，并发运行两个项目时会互相覆盖权限规则。\n2. `run_shell` 直接调用同步 `subprocess.run()`，长命令会阻塞 `asyncio` 事件循环。\n\n我已整理好修复补丁，下一步需要运行 pytest 回归验证多项目隔离。',timestamp:'11月27日 14:13',toolCalls:[
      {id:'tc-1',name:'read_file',summary:'读取了 agents/tools.py 并检查全局缓存定义',args:'{"path": "agents/tools.py", "start_line": 560, "end_line": 670}',output:'Found module-level _cached_rules and sync subprocess.run() at line 588.',status:'success',durationMs:180},
      {id:'tc-2',name:'edit_file',summary:'修改了 agents/tools.py（按 workspace_dir 分键缓存）',args:'{"path": "agents/tools.py"}',output:'Applied 1 hunk cleanly.',status:'success',durationMs:240,diff:{filePath:'agents/tools.py',lines:[{type:'context',content:'from pathlib import Path'},{type:'del',content:'_cached_rules: dict | None = None'},{type:'add',content:'_cached_rules_by_ws: dict[Path, dict] = {}'},{type:'context',content:''},{type:'del',content:'result = subprocess.run(cmd, shell=True, cwd=cwd)'},{type:'add',content:'proc = await asyncio.create_subprocess_shell(cmd, cwd=cwd, start_new_session=True)'}]}},
    ],approval:{requestId:'req-pytest-001',toolName:'run_shell',commandOrPath:'pytest tests/test_isolation.py tests/test_session_manager.py -q',reason:'执行多项目隔离与 SessionManager 断线重连回归测试',status:'pending'}},
  ],traces:[
    {id:'tr-0',round:0,role:'system',lane:'input',label:'系统',summary:'初始系统提示词',detail:'You are ResearchAgent, a local-first engineering and research assistant...',durationMs:40,timestamp:'14:12:01'},
    {id:'tr-1',round:1,role:'user',lane:'input',label:'用户',summary:'帮我检查 agents/tools.py 里的权限缓存和 Shell 执行，看看单进程多项目并发时有没有阻塞和串台问题。',detail:'帮我检查 agents/tools.py 里的权限缓存和 Shell 执行，看看单进程多项目并发时有没有阻塞和串台问题。',durationMs:25,timestamp:'14:12:01'},
    {id:'tr-2',round:1,role:'context',lane:'input',label:'上下文',summary:'Current runtime context. Workspace: ~/projects/ResearchAgent · Loaded CLAUDE.md & 2 workspace rules.',detail:'Workspace: ~/projects/ResearchAgent\nRules: .research/rules/architecture.md, CLAUDE.md',durationMs:65,timestamp:'14:12:01'},
    {id:'tr-3',round:1,role:'assistant',lane:'model',label:'助手',isThinking:true,summary:'The user wants to audit agents/tools.py for module-level cache pollution and blocking subprocess calls...',detail:'Thinking: Let me inspect agents/tools.py around permission caching and shell execution first.',durationMs:920,timestamp:'14:12:02'},
    {id:'tr-4',round:1,role:'tool',lane:'tool',label:'工具',toolName:'read_file',summary:'{"path": "agents/tools.py", "start_line": 560, "end_line": 670}',resultPreview:'110 lines read (3.8 KB)',detail:'Read agents/tools.py lines 560-670 successfully.',durationMs:180,timestamp:'14:12:03'},
    {id:'tr-5',round:1,role:'tool',lane:'tool',label:'工具',toolName:'grep_search',summary:'{"pattern": "subprocess.run", "path": "agents/"}',resultPreview:'WEB_BLOCKED / 2 matches found in tools.py',isError:false,detail:'agents/tools.py:451 (grep)\nagents/tools.py:588 (shell)',durationMs:310,timestamp:'14:12:04'},
    {id:'tr-6',round:2,role:'assistant',lane:'model',label:'助手',isThinking:false,summary:'已定位到两处核心问题，准备修改 agents/tools.py 并发起回归测试审批。',detail:'Generated patch for agents/tools.py and requested approval for pytest.',durationMs:1350,timestamp:'14:12:06'},
    {id:'tr-7',round:2,role:'tool',lane:'tool',label:'工具',toolName:'run_shell',summary:'{"command": "pytest tests/test_isolation.py -q"}',resultPreview:'WAITING_APPROVAL',isError:true,detail:'Awaiting user decision for request_id=req-pytest-001',durationMs:480,timestamp:'14:12:08'},
  ]},
  {id:'sess-homepage-mvp',workspaceId:'ws-homepage',title:'搭建个人主页 MVP 版本',modeLabel:'标准模式',updatedAt:'1个月',protocol:'anthropic',model:'Claude-3.7-Sonnet',status:'stopped',metrics:{rounds:1,steps:3,tokensPerSec:38,contextTokens:{system:1500,tools:2800,messages:13900},cacheHitRate:91,foldedCount:0},messages:[{id:'hp-1',role:'user',content:'帮我梳理个人主页的模块结构，突出 ResearchAgent 和 ShopSage 两个核心项目。',timestamp:'10月26日 15:30'},{id:'hp-2',role:'assistant',statusLabel:'已停止',content:'已为你生成极简双栏个人主页结构，包含 Hero 简介、旗舰项目指标卡片和开源博客链接。',timestamp:'10月26日 15:31',toolCalls:[{id:'tc-hp-1',name:'list_dir',summary:'执行了目录扫描并检查现有静态资源',args:'{"path": "."}',output:'index.html, assets/, projects.json',status:'success',durationMs:95}]}],traces:[{id:'tr-hp-1',round:1,role:'user',lane:'input',label:'用户',summary:'帮我梳理个人主页的模块结构，突出 ResearchAgent 和 ShopSage 两个核心项目。',detail:'帮我梳理个人主页的模块结构，突出 ResearchAgent 和 ShopSage 两个核心项目。',durationMs:20,timestamp:'15:30:01'},{id:'tr-hp-2',round:1,role:'assistant',lane:'model',label:'助手',summary:'Planning portfolio layout with metrics cards for ResearchAgent and ShopSage.',detail:'Structured sections: Bio, Projects, Publications/Awards, Blog.',durationMs:780,timestamp:'15:30:02'}]},
  {id:'sess-ungrouped-1',workspaceId:'ws-ungrouped',title:'Markdown 转 LaTeX PDF 工具',modeLabel:'标准模式',updatedAt:'23天',protocol:'openai',model:'DeepSeek-V4-Pro High',status:'idle',metrics:{rounds:1,steps:2,tokensPerSec:45,contextTokens:{system:1500,tools:2100,messages:5500},cacheHitRate:76,foldedCount:0},messages:[{id:'ug-1',role:'user',content:'写一个把 Obsidian Markdown 笔记批量转成 XeLaTeX PDF 的脚本。',timestamp:'11月04日 19:20'},{id:'ug-2',role:'assistant',content:'推荐使用 Pandoc + XeLaTeX 模板，并处理中文断行与代码块高亮。',timestamp:'11月04日 19:21'}],traces:[]},
];
export const INITIAL_SKILLS:SkillItem[]=[
{id:'sk-1',name:'diagnose-isolation',scope:'workspace',description:'自动审计 Python 异步服务中的全局缓存污染、阻塞 I/O 与 ContextVar 泄漏。',enabled:true,triggerCount:6,sourcePath:'.research/skills/diagnose-isolation.md'},
{id:'sk-2',name:'latex-resume-polish',scope:'global',description:'按量化指标与工程取舍框架优化纯 LaTeX 技术简历条目。',enabled:true,triggerCount:14,sourcePath:'~/.researchagent/skills/latex-resume-polish.md'},
{id:'sk-3',name:'rag-eval-harness',scope:'workspace',description:'生成检索召回率、Precision@K 与忠实度评估脚本。',enabled:false,triggerCount:2,sourcePath:'.research/skills/rag-eval-harness.md'},
];
export const INITIAL_MCP_SERVERS:McpServerItem[]=[
{id:'mcp-1',name:'context7',transport:'stdio',command:'npx -y @upstash/context7-mcp',status:'connected',toolsCount:2,scope:'global'},
{id:'mcp-2',name:'playwright',transport:'stdio',command:'npx -y @playwright/mcp@latest',status:'connected',toolsCount:18,scope:'global'},
{id:'mcp-3',name:'local-qdrant',transport:'sse',command:'http://127.0.0.1:6333/mcp/sse',status:'disconnected',toolsCount:4,scope:'workspace'},
];
