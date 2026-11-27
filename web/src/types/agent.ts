export type NavView = 'chat' | 'skills' | 'mcp';
export type SessionTab = 'chat' | 'trace';
export type TraceViewMode = 'duration' | 'round' | 'call';
export type PermissionMode = 'workspace_write' | 'read_only' | 'full_access';
export type RunStatus = 'idle' | 'running' | 'waiting_approval' | 'stopped' | 'error';
export interface Workspace {id:string;name:string;path:string;isDefault?:boolean;isUngrouped?:boolean;}
export interface ToolCallItem {id:string;name:string;summary:string;args:string;output:string;status:'success'|'error'|'running'|'pending_approval';durationMs:number;diff?:{filePath:string;lines:Array<{type:'context'|'add'|'del';content:string}>};}
export interface PendingApproval {requestId:string;toolName:string;commandOrPath:string;reason:string;status:'pending'|'approved'|'rejected';}
export interface ChatMessage {id:string;role:'user'|'assistant'|'system_notice';content:string;timestamp:string;checkpoint?:{traceCount:number;metrics:SessionMetrics};statusLabel?:string;toolCalls?:ToolCallItem[];approval?:PendingApproval;}
export type TraceRole='system'|'user'|'context'|'assistant'|'tool';
export type TraceLane='input'|'model'|'tool';
export interface TraceEvent {id:string;round:number;role:TraceRole;lane:TraceLane;label:string;summary:string;detail:string;toolName?:string;resultPreview?:string;isError?:boolean;isThinking?:boolean;durationMs:number;timestamp:string;}
export interface SessionMetrics {rounds:number;steps:number;tokensPerSec:number;contextTokens:{system:number;tools:number;messages:number};cacheHitRate:number;foldedCount:number;}
export interface Session {id:string;parentSessionId?:string;forkFromMessageId?:string;workspaceId:string;title:string;modeLabel:string;updatedAt:string;protocol:'openai'|'anthropic';model:string;status:RunStatus;messages:ChatMessage[];traces:TraceEvent[];metrics:SessionMetrics;}
export interface SkillItem {id:string;name:string;scope:'global'|'workspace';description:string;enabled:boolean;triggerCount:number;sourcePath:string;}
export interface McpServerItem {id:string;name:string;transport:'stdio'|'sse';command:string;status:'connected'|'disconnected'|'error';toolsCount:number;scope:'global'|'workspace';}
