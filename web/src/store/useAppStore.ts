import { create } from 'zustand';
import { AVAILABLE_MODELS, INITIAL_MCP_SERVERS, INITIAL_SESSIONS, INITIAL_SKILLS, INITIAL_WORKSPACES } from '../data/mockData';
import { McpServerItem, NavView, PermissionMode, Session, SessionTab, SkillItem, TraceViewMode, Workspace } from '../types/agent';
interface AppState {
  sidebarCollapsed:boolean;inspectorOpen:boolean;activeNav:NavView;activeSessionTab:SessionTab;traceViewMode:TraceViewMode;traceSearch:string;workspaceSearch:string;selectedTraceId:string|null;
  workspaces:Workspace[];expandedWorkspaceIds:string[];activeWorkspaceId:string;sessions:Session[];activeSessionId:string|null;skills:SkillItem[];mcpServers:McpServerItem[];
  permissionMode:PermissionMode;selectedModel:string;toastMessage:string|null;
  forkSession:(sessionId:string,messageId:string)=>void;
  toggleSidebar:()=>void;toggleInspector:()=>void;setActiveNav:(nav:NavView)=>void;setActiveSessionTab:(tab:SessionTab)=>void;setTraceViewMode:(mode:TraceViewMode)=>void;setTraceSearch:(q:string)=>void;setWorkspaceSearch:(q:string)=>void;setSelectedTraceId:(id:string|null)=>void;toggleWorkspaceExpand:(wsId:string)=>void;selectSession:(sessionId:string)=>void;startNewTask:(workspaceId?:string)=>void;addMockWorkspace:(name:string)=>void;setPermissionMode:(mode:PermissionMode)=>void;setSelectedModel:(model:string)=>void;sendMessage:(text:string)=>void;stopRun:()=>void;resolveApproval:(sessionId:string,requestId:string,decision:'approved'|'rejected')=>void;toggleSkill:(skillId:string)=>void;toggleMcpServer:(serverId:string)=>void;showToast:(msg:string)=>void;
}
// Each mock session has its own timer, so switching sessions does not cancel another task.
const mockTimers = new Map<string,number>();
// Each demo session starts with a single assistant reply.
const initialSessions = INITIAL_SESSIONS.map(session => ({
  ...session,
  messages: session.messages.map(message => session.status === 'idle' && message.role === 'assistant'
    ? { ...message, checkpoint: { traceCount: session.traces.length, metrics: session.metrics } }
    : message),
}));

export const useAppStore = create<AppState>((set,get)=>({
  sidebarCollapsed:false,inspectorOpen:false,activeNav:'chat',activeSessionTab:'chat',traceViewMode:'duration',traceSearch:'',workspaceSearch:'',selectedTraceId:null,
  workspaces:INITIAL_WORKSPACES,expandedWorkspaceIds:['ws-research','ws-homepage','ws-ungrouped'],activeWorkspaceId:'ws-research',sessions:initialSessions,activeSessionId:'sess-006-isolation',skills:INITIAL_SKILLS,mcpServers:INITIAL_MCP_SERVERS,
  permissionMode:'workspace_write',selectedModel:AVAILABLE_MODELS[0].id,toastMessage:null,
  toggleSidebar:()=>set(s=>({sidebarCollapsed:!s.sidebarCollapsed})),toggleInspector:()=>set(s=>({inspectorOpen:!s.inspectorOpen})),setActiveNav:nav=>set({activeNav:nav}),setActiveSessionTab:tab=>set({activeSessionTab:tab}),setTraceViewMode:mode=>set({traceViewMode:mode}),setTraceSearch:q=>set({traceSearch:q}),setWorkspaceSearch:q=>set({workspaceSearch:q}),setSelectedTraceId:id=>set({selectedTraceId:id}),
  toggleWorkspaceExpand:wsId=>set(s=>({expandedWorkspaceIds:s.expandedWorkspaceIds.includes(wsId)?s.expandedWorkspaceIds.filter(id=>id!==wsId):[...s.expandedWorkspaceIds,wsId]})),
  selectSession:sessionId=>{const target=get().sessions.find(s=>s.id===sessionId);if(!target)return;set({activeNav:'chat',activeSessionId:sessionId,activeWorkspaceId:target.workspaceId,selectedModel:target.model,selectedTraceId:null,traceSearch:'',toastMessage:null});},
  forkSession:(sessionId,messageId)=>{
    const state = get();
    const source = state.sessions.find(session => session.id === sessionId);
    if (!source) return;
    const index = source.messages.findIndex(message => message.id === messageId);
    const checkpoint = source.messages[index]?.checkpoint;
    if (!checkpoint) return;

    const branch: Session = {
      ...source,
      id: crypto.randomUUID(),
      title: '分支 · ' + source.title,
      parentSessionId: source.id,
      forkFromMessageId: messageId,
      updatedAt: '刚刚',
      status: 'idle',
      messages: structuredClone(source.messages.slice(0, index + 1)),
      traces: structuredClone(source.traces.slice(0, checkpoint.traceCount)),
      metrics: { ...checkpoint.metrics },
    };
    set({
      sessions: [branch, ...state.sessions],
      activeSessionId: branch.id,
      activeWorkspaceId: branch.workspaceId,
      activeNav: 'chat',
      activeSessionTab: 'chat',
      selectedModel: branch.model,
      selectedTraceId: null,
      traceSearch: '',
      expandedWorkspaceIds: Array.from(new Set([...state.expandedWorkspaceIds, branch.workspaceId])),
    });
    get().showToast('已在当前工作区创建分支');
  },
  startNewTask:workspaceId=>set(s=>({activeNav:'chat',activeSessionId:null,activeSessionTab:'chat',activeWorkspaceId:workspaceId??s.activeWorkspaceId,selectedTraceId:null,traceSearch:'',toastMessage:null})),
  addMockWorkspace:name=>{const trimmed=name.trim();if(!trimmed)return;const newWs:Workspace={id:`ws-${Date.now()}`,name:trimmed,path:`~/projects/${trimmed}`};set(s=>({workspaces:[newWs,...s.workspaces],expandedWorkspaceIds:[...s.expandedWorkspaceIds,newWs.id],activeWorkspaceId:newWs.id,activeSessionId:null,activeSessionTab:'chat',activeNav:'chat',traceSearch:'',selectedTraceId:null}));get().showToast(`已添加工作区：${newWs.path}`);},
  setPermissionMode:mode=>set({permissionMode:mode}),setSelectedModel:model=>set({selectedModel:model}),
  showToast:msg=>{set({toastMessage:msg});window.setTimeout(()=>{if(get().toastMessage===msg)set({toastMessage:null});},2400);},
  sendMessage:rawText=>{
    const text=rawText.trim();if(!text)return;const state=get();const nowTime=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});const modelObj=AVAILABLE_MODELS.find(m=>m.id===state.selectedModel)??AVAILABLE_MODELS[0];
    let targetSessionId=state.activeSessionId;
    const existing=state.sessions.find(s=>s.id===targetSessionId);
    if(existing?.status==='running'||existing?.status==='waiting_approval'){get().showToast('请先完成或停止当前回合');return;}
    if(!targetSessionId){targetSessionId=`sess-${crypto.randomUUID()}`;const newSession:Session={id:targetSessionId,workspaceId:state.activeWorkspaceId,title:text.slice(0,22),modeLabel:'标准模式',updatedAt:'刚刚',protocol:modelObj.protocol,model:modelObj.id,status:'running',metrics:{rounds:1,steps:1,tokensPerSec:46,contextTokens:{system:1500,tools:1800,messages:900},cacheHitRate:88,foldedCount:0},messages:[],traces:[{id:`tr-sys-${Date.now()}`,round:0,role:'system',lane:'input',label:'系统',summary:'初始系统提示词',detail:'ResearchAgent System Prompt loaded.',durationMs:35,timestamp:nowTime}]};set(s=>({sessions:[newSession,...s.sessions],activeSessionId:targetSessionId,expandedWorkspaceIds:s.expandedWorkspaceIds.includes(s.activeWorkspaceId)?s.expandedWorkspaceIds:[...s.expandedWorkspaceIds,s.activeWorkspaceId]}));}
    const sessionId = targetSessionId;
    const round = (existing?.metrics.rounds ?? 0) + 1;
    const userId = crypto.randomUUID();
    const replyId = crypto.randomUUID();
    const content = `已收到任务：「${text}」。\n\n当前为本地 Mock 原型模式，已模拟完成工作区上下文扫描与工具调用，你可以切换到顶部「轨迹」Tab 查看本轮泳道时间轴。`;
    set(s => ({sessions:s.sessions.map(sess => sess.id !== sessionId ? sess : {
      ...sess, status:'running', updatedAt:'刚刚', model:modelObj.id, protocol:modelObj.protocol,
      metrics:{...sess.metrics, rounds:round},
      messages:[...sess.messages, {id:userId,role:'user',content:text,timestamp:nowTime}],
      traces:[...sess.traces, {id:'tr-'+userId,round,role:'user',lane:'input',label:'用户',summary:text,detail:text,durationMs:20,timestamp:nowTime}],
    })}));
    // Deliver the mock response in four chunks to the originating session.
    let chunk = 0;
    const timer = window.setInterval(() => {
      chunk++;
      const done = chunk === 4;
      if (done) {
        window.clearInterval(timer);
        mockTimers.delete(sessionId);
      }
      set(s => ({sessions:s.sessions.map(sess => {
        if (sess.id !== sessionId || sess.status !== 'running') return sess;
        const partial = {id:replyId,role:'assistant' as const,timestamp:nowTime,
          content:content.slice(0,Math.ceil(content.length * chunk / 4))};
        const messages = chunk === 1 ? [...sess.messages,partial] :
          sess.messages.map(message => message.id === replyId ? partial : message);
        if (!done) return {...sess,messages};
        const traces: Session['traces'] = [...sess.traces,
          {id:'tr-m-'+replyId,round,role:'assistant',lane:'model',label:'助手',isThinking:true,summary:`Analyzing request: ${text}`,detail:`Mock reasoning trace for "${text}".`,durationMs:640,timestamp:nowTime},
          {id:'tr-t-'+replyId,round,role:'tool',lane:'tool',label:'工具',toolName:'workspace_scan',summary:'{"include_rules": true}',resultPreview:'OK (210ms)',detail:'Loaded CLAUDE.md and workspace metadata.',durationMs:210,timestamp:nowTime},
        ];
        const metrics = {...sess.metrics,steps:sess.metrics.steps+2};
        const reply = {...partial,checkpoint:{traceCount:traces.length,metrics},toolCalls:[
          {id:'tc-'+replyId,name:'workspace_scan',summary:'扫描了当前工作区目录与规则文件',args:'{"include_rules": true}',output:'Loaded CLAUDE.md and workspace metadata.',status:'success' as const,durationMs:210},
        ]};
        return {...sess,status:'idle',metrics,traces,messages:messages.map(message=>message.id===replyId?reply:message)};
      })}));
    }, 225);
    mockTimers.set(sessionId,timer);
  },
  stopRun:()=>{const activeId=get().activeSessionId;if(!get().sessions.some(s=>s.id===activeId && s.status==='running') || !activeId)return;const timer=mockTimers.get(activeId);if(timer!==undefined){window.clearInterval(timer);mockTimers.delete(activeId);}set(s=>({sessions:s.sessions.map(sess=>sess.id===activeId?{...sess,status:'stopped',messages:[...sess.messages.map(m=>m.approval?.status==='pending'?{...m,approval:{...m.approval,status:'rejected' as const}}:m),{id:`msg-stop-${Date.now()}`,role:'system_notice',content:'已停止当前回合（子进程与待决审批已安全收尾）',timestamp:''}]}:sess)}));},
  resolveApproval:(sessionId,requestId,decision)=>{const sess=get().sessions.find(s=>s.id===sessionId);if(!sess?.messages.some(m=>m.approval?.requestId===requestId&&m.approval.status==='pending'))return;const nowTime=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});set(s=>({sessions:s.sessions.map(sess=>sess.id!==sessionId?sess:{...sess,status:'idle',messages:sess.messages.map(m=>m.approval?.requestId===requestId?{...m,checkpoint:{traceCount:sess.traces.length+1,metrics:sess.metrics},approval:{...m.approval,status:decision}}:m),traces:[...sess.traces,{id:`tr-app-${Date.now()}`,round:sess.metrics.rounds,role:'tool',lane:'tool',label:'工具',toolName:'run_shell',summary:`审批结果：${decision==='approved'?'已允许执行':'已拒绝执行'}`,resultPreview:decision==='approved'?'9 passed in 1.42s':'REJECTED_BY_USER',isError:decision==='rejected',detail:decision==='approved'?'pytest tests/test_isolation.py tests/test_session_manager.py -q\n9 passed in 1.42s':'User rejected shell execution.',durationMs:decision==='approved'?1420:10,timestamp:nowTime}]})}));get().showToast(decision==='approved'?'已批准并完成工具执行（Mock）':'已拒绝该工具调用');},
  toggleSkill:skillId=>set(s=>({skills:s.skills.map(sk=>sk.id===skillId?{...sk,enabled:!sk.enabled}:sk)})),toggleMcpServer:serverId=>set(s=>({mcpServers:s.mcpServers.map(m=>m.id===serverId?{...m,status:m.status==='connected'?'disconnected':'connected'}:m)})),
}));
