/**
 * Full-page view for scheduled task execution history
 */

import React, { useState, useEffect, useCallback, useRef } from "react";
// useRef is used for hasInitializedRef and hasActiveRef (stable polling interval)
import { MoreHorizontal, FileText, Download, ArrowLeft, ChevronRight, ChevronLeft, MessageCircle } from "lucide-react";
import type { ScheduledTask, TaskExecution, ArtifactInfo } from "@/lib/types/scheduled-tasks";
import { transformApiExecution } from "@/lib/types/scheduled-tasks";
import { Header } from "@/lib/components/header";
import { Button, Label } from "@/lib/components/ui";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/lib/components/ui";
import { useNavigate } from "react-router-dom";
import { useChatContext } from "@/lib/hooks";
import { api } from "@/lib/api/client";
import { ContentRenderer } from "@/lib/components/chat/preview/ContentRenderer";
import { getRenderType } from "@/lib/components/chat/preview/previewUtils";
import { MarkdownHTMLConverter } from "@/lib/components/common/MarkdownHTMLConverter";
import { formatEpochTimestamp, formatDuration } from "@/lib/utils/format";

interface TaskExecutionHistoryPageProps {
    task: ScheduledTask;
    onBack: () => void;
    onEdit: (task: ScheduledTask) => void;
    onDelete: (id: string, name: string) => void;
}

export const TaskExecutionHistoryPage: React.FC<TaskExecutionHistoryPageProps> = ({ task, onBack, onEdit, onDelete }) => {
    const navigate = useNavigate();
    const { addNotification, handleSwitchSession } = useChatContext();
    const [executions, setExecutions] = useState<TaskExecution[]>([]);
    const [selectedExecution, setSelectedExecution] = useState<TaskExecution | null>(null);
    const [isLoading, setIsLoading] = useState(false);
    const [previewArtifact, setPreviewArtifact] = useState<ArtifactInfo | null>(null);
    const [artifactContent, setArtifactContent] = useState<string | null>(null);
    const [loadingArtifact, setLoadingArtifact] = useState(false);
    const hasInitializedRef = useRef(false);

    const fetchExecutions = useCallback(
        async (showLoading = true) => {
            if (showLoading) setIsLoading(true);
            try {
                const data = await api.webui.get(`/api/v1/scheduled-tasks/${task.id}/executions`);
                // API returns { executions: [], total: number, skip: number, limit: number }
                // Transform from snake_case to camelCase
                const executionsList = (data.executions || []).map(transformApiExecution);
                setExecutions(executionsList);

                // Auto-select first on initial load, or refresh the currently selected execution
                setSelectedExecution(prev => {
                    if (!prev && executionsList.length > 0 && !hasInitializedRef.current) {
                        hasInitializedRef.current = true;
                        return executionsList[0];
                    }
                    if (!prev) return prev;
                    const updated = executionsList.find((e: TaskExecution) => e.id === prev.id);
                    return updated || prev;
                });
            } catch (error) {
                const errorMsg = error instanceof Error ? error.message : "Failed to load execution history";
                addNotification(errorMsg, "warning");
            } finally {
                if (showLoading) setIsLoading(false);
            }
        },
        [task.id, addNotification]
    );

    useEffect(() => {
        fetchExecutions();
    }, [fetchExecutions]);

    // Keep a ref that tracks whether any execution is active so the polling
    // effect below doesn't need `executions` in its dependency array (which
    // would tear down and re-create the timer on every poll response).
    const hasActiveRef = useRef(false);
    useEffect(() => {
        hasActiveRef.current = executions.some(e => e.status === "running" || e.status === "pending");
    }, [executions]);

    // Smart polling: fast when executions are running, slow otherwise, paused when tab hidden.
    // Stable effect — only depends on fetchExecutions (which is memoised with useCallback).
    useEffect(() => {
        let timerId: ReturnType<typeof setTimeout>;

        const poll = () => {
            if (!document.hidden) {
                fetchExecutions(false);
            }
            timerId = setTimeout(poll, hasActiveRef.current ? 5_000 : 30_000);
        };

        timerId = setTimeout(poll, hasActiveRef.current ? 5_000 : 30_000);

        // Resume polling immediately when tab becomes visible
        const onVisibilityChange = () => {
            if (!document.hidden) {
                fetchExecutions(false);
            }
        };
        document.addEventListener("visibilitychange", onVisibilityChange);

        return () => {
            clearTimeout(timerId);
            document.removeEventListener("visibilitychange", onVisibilityChange);
        };
    }, [fetchExecutions]);

    const getStatusBadge = (status: string) => {
        const statusConfig = {
            completed: { bg: "bg-(--color-success-w20)", text: "text-(--color-success-wMain)", label: "Completed" },
            failed: { bg: "bg-(--color-error-w20)", text: "text-(--color-error-wMain)", label: "Failed" },
            running: { bg: "bg-(--color-info-w20)", text: "text-(--color-info-wMain)", label: "Running" },
            timeout: { bg: "bg-(--color-warning-w20)", text: "text-(--color-warning-wMain)", label: "Timeout" },
        };
        const config = statusConfig[status as keyof typeof statusConfig] || statusConfig.failed;
        return <span className={`rounded-full px-2 py-0.5 text-xs ${config.bg} ${config.text}`}>{config.label}</span>;
    };

    const renderResponse = (execution: TaskExecution) => {
        const summary = execution.resultSummary;
        if (!summary) return <p className="text-(--secondary-text-wMain)">No response available</p>;

        // For RUN_BASED sessions, show agentResponse with full markdown formatting
        if (summary.agentResponse) {
            return (
                <div className="rounded bg-(--secondary-w10) p-3 text-sm">
                    <MarkdownHTMLConverter>{summary.agentResponse}</MarkdownHTMLConverter>
                </div>
            );
        }

        // For PERSISTENT sessions, show message history with full markdown formatting
        if (summary.messages && Array.isArray(summary.messages) && summary.messages.length > 0) {
            return (
                <div className="space-y-3">
                    {summary.messages.map((msg: { role: string; text: string }, idx: number) => (
                        <div key={idx} className="space-y-1">
                            <div className="text-xs font-medium text-(--secondary-text-wMain) capitalize">{msg.role || "Unknown"}</div>
                            <div className="rounded bg-(--secondary-w10) p-3 text-sm">
                                <MarkdownHTMLConverter>{msg.text || "No content"}</MarkdownHTMLConverter>
                            </div>
                        </div>
                    ))}
                </div>
            );
        }

        return <p className="text-(--secondary-text-wMain)">No response data available</p>;
    };

    /**
     * Convert an artifact URI to an API path the browser can fetch.
     * `artifact://{session_id}/{filename}` → `/api/v1/artifacts/scheduled/{session_id}/{filename}`
     */
    const resolveArtifactUri = (uri: string): string => {
        if (uri.startsWith("artifact://")) {
            const path = uri.slice("artifact://".length);
            return `/api/v1/artifacts/scheduled/${path}`;
        }
        return uri;
    };

    const handlePreviewArtifact = async (artifact: ArtifactInfo) => {
        // Toggle: if clicking the same artifact, close the panel
        if (previewArtifact && previewArtifact.name === artifact.name) {
            setPreviewArtifact(null);
            setArtifactContent(null);
            return;
        }

        setPreviewArtifact(artifact);
        setArtifactContent(null);

        if (artifact.uri) {
            setLoadingArtifact(true);
            try {
                const apiPath = resolveArtifactUri(artifact.uri);
                const response = await api.webui.get(apiPath, { fullResponse: true });

                const contentType = response.headers?.get("content-type") || "";
                const isText = contentType.startsWith("text/") || contentType.includes("json") || contentType.includes("xml") || contentType.includes("javascript") || contentType.includes("csv");
                if (isText) {
                    const content = await response.text();
                    setArtifactContent(content);
                } else {
                    const blob = await response.blob();
                    const url = URL.createObjectURL(blob);
                    setArtifactContent(url);
                }
            } catch (error) {
                const errorMsg = error instanceof Error ? error.message : "Failed to load artifact content";
                addNotification(errorMsg, "warning");
            } finally {
                setLoadingArtifact(false);
            }
        }
    };

    const renderArtifacts = (execution: TaskExecution) => {
        // Artifacts can be in execution.artifacts (top-level) or execution.resultSummary.artifacts
        const topLevelArtifacts = execution.artifacts || [];
        const summaryArtifacts = execution.resultSummary?.artifacts || [];

        // Combine and normalize artifacts
        const allArtifacts = [...topLevelArtifacts.map(a => (typeof a === "string" ? { name: a, uri: `artifact://${a}` } : a)), ...summaryArtifacts];

        if (allArtifacts.length === 0) {
            return <p className="text-sm text-(--secondary-text-wMain)">No artifacts generated</p>;
        }

        return (
            <div className="space-y-2">
                {allArtifacts.map((artifact, idx: number) => {
                    const isViewable = artifact.uri?.startsWith("http") || artifact.uri?.startsWith("/") || artifact.uri?.startsWith("artifact://");
                    const filename = artifact.name || artifact.uri?.split("/").pop() || `artifact-${idx + 1}`;

                    const artifactInfo: ArtifactInfo = {
                        name: filename,
                        uri: artifact.uri || "",
                    };

                    const isCurrentlyPreviewed = previewArtifact?.name === filename;

                    return (
                        <button
                            key={idx}
                            onClick={() => isViewable && handlePreviewArtifact(artifactInfo)}
                            disabled={!isViewable}
                            className={`group flex w-full items-center justify-between rounded p-3 text-left transition-colors ${
                                isViewable ? (isCurrentlyPreviewed ? "border border-(--primary-w20) bg-(--primary-w10)" : "cursor-pointer bg-(--secondary-w10) hover:bg-(--primary-w10)") : "cursor-not-allowed bg-(--secondary-w10) opacity-60"
                            }`}
                        >
                            <div className="flex min-w-0 flex-1 items-center gap-2">
                                <FileText className="size-4 flex-shrink-0 text-(--secondary-text-wMain)" />
                                <span className={`truncate text-sm ${isViewable ? (isCurrentlyPreviewed ? "font-medium text-(--primary-wMain)" : "group-hover:text-(--primary-wMain)") : ""}`} title={filename}>
                                    {filename}
                                </span>
                            </div>
                            {isViewable &&
                                (isCurrentlyPreviewed ? (
                                    <ChevronLeft className="size-4 text-(--primary-wMain) transition-colors" />
                                ) : (
                                    <ChevronRight className="size-4 text-(--secondary-text-wMain) transition-colors group-hover:text-(--primary-wMain)" />
                                ))}
                        </button>
                    );
                })}
            </div>
        );
    };

    const formatScheduleExpression = (task: ScheduledTask) => {
        if (task.scheduleType === "cron") {
            return task.scheduleExpression;
        } else if (task.scheduleType === "interval") {
            return `Every ${task.scheduleExpression}`;
        } else if (task.scheduleType === "one_time") {
            // Parse ISO timestamp and format it
            try {
                const date = new Date(task.scheduleExpression);
                return date.toLocaleString();
            } catch {
                return task.scheduleExpression;
            }
        }
        return task.scheduleExpression;
    };

    return (
        <div className="flex h-full flex-col">
            {/* Header with Breadcrumbs */}
            <Header
                title={task.name}
                breadcrumbs={[{ label: "Scheduled Tasks", onClick: onBack }, { label: task.name }]}
                buttons={[
                    <DropdownMenu key="actions-menu">
                        <DropdownMenuTrigger asChild>
                            <Button variant="ghost" size="sm">
                                <MoreHorizontal className="h-4 w-4" />
                            </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end">
                            <DropdownMenuItem onClick={() => onEdit(task)}>Edit Task</DropdownMenuItem>
                            <DropdownMenuItem onClick={() => onDelete(task.id, task.name)}>Delete Task</DropdownMenuItem>
                        </DropdownMenuContent>
                    </DropdownMenu>,
                ]}
            />

            {/* Content */}
            <div className="flex min-h-0 flex-1">
                {/* Left Sidebar - Execution List */}
                <div className="w-[300px] overflow-y-auto border-r">
                    <div className="p-4">
                        <h3 className="mb-3 text-sm font-semibold text-(--secondary-text-wMain)">Executions ({executions.length})</h3>
                        {isLoading ? (
                            <div className="flex items-center justify-center p-8">
                                <div className="border-primary size-6 animate-spin rounded-full border-2 border-t-transparent" />
                            </div>
                        ) : executions.length === 0 ? (
                            <p className="p-4 text-center text-sm text-(--secondary-text-wMain)">No executions yet</p>
                        ) : (
                            <div className="space-y-2">
                                {executions.map(execution => {
                                    const isSelected = selectedExecution?.id === execution.id;

                                    return (
                                        <button
                                            key={execution.id}
                                            onClick={() => setSelectedExecution(execution)}
                                            className={`w-full rounded p-3 text-left transition-colors ${isSelected ? "border border-(--primary-w20) bg-(--primary-w10)" : "hover:bg-(--secondary-w20)"}`}
                                        >
                                            <div className="mb-2 flex items-center justify-between">
                                                {getStatusBadge(execution.status)}
                                                <span className="text-xs text-(--secondary-text-wMain)">{execution.durationMs ? formatDuration(execution.durationMs) : "-"}</span>
                                            </div>
                                            <span className="block text-xs text-(--secondary-text-wMain)">{execution.startedAt ? formatEpochTimestamp(execution.startedAt) : "Pending"}</span>
                                        </button>
                                    );
                                })}
                            </div>
                        )}
                    </div>
                </div>

                {/* Center Panel - Execution Details */}
                <div className={`flex-1 overflow-y-auto ${previewArtifact ? "border-r" : ""}`}>
                    {selectedExecution ? (
                        <div className="p-6">
                            <div className="mx-auto max-w-4xl space-y-6">
                                {/* Header */}
                                <div className="flex items-center justify-between">
                                    <h2 className="text-lg font-semibold">Execution Details</h2>
                                    <div className="flex items-center gap-2">
                                        <Button
                                            variant="ghost"
                                            size="sm"
                                            onClick={async () => {
                                                await handleSwitchSession(`scheduled_${selectedExecution.id}`);
                                                navigate("/chat");
                                            }}
                                        >
                                            <MessageCircle className="mr-1 h-4 w-4" />
                                            Go to Chat
                                        </Button>
                                        {getStatusBadge(selectedExecution.status)}
                                    </div>
                                </div>

                                {/* Execution Metadata */}
                                <div className="grid grid-cols-2 gap-4 rounded bg-(--secondary-w10) p-4">
                                    <div>
                                        <Label className="text-xs text-(--secondary-text-wMain)">Started</Label>
                                        <div className="mt-1 text-sm">{selectedExecution.startedAt ? formatEpochTimestamp(selectedExecution.startedAt) : "Pending"}</div>
                                    </div>
                                    {selectedExecution.completedAt && (
                                        <div>
                                            <Label className="text-xs text-(--secondary-text-wMain)">Completed</Label>
                                            <div className="mt-1 text-sm">{formatEpochTimestamp(selectedExecution.completedAt)}</div>
                                        </div>
                                    )}
                                    {selectedExecution.durationMs && (
                                        <div>
                                            <Label className="text-xs text-(--secondary-text-wMain)">Duration</Label>
                                            <div className="mt-1 text-sm">{formatDuration(selectedExecution.durationMs)}</div>
                                        </div>
                                    )}
                                    {selectedExecution.retryCount > 0 && (
                                        <div>
                                            <Label className="text-xs text-(--secondary-text-wMain)">Retries</Label>
                                            <div className="mt-1 text-sm">{selectedExecution.retryCount}</div>
                                        </div>
                                    )}
                                </div>

                                {/* Error Message */}
                                {selectedExecution.errorMessage && (
                                    <div className="space-y-2">
                                        <Label className="text-(--color-secondaryText-wMain)">Error</Label>
                                        <div className="rounded bg-(--color-error-w20) p-3 text-sm break-words whitespace-pre-wrap text-(--color-error-wMain)">{selectedExecution.errorMessage}</div>
                                    </div>
                                )}

                                {/* Agent Response */}
                                <div className="space-y-2">
                                    <Label className="text-(--color-secondaryText-wMain)">Response (Summary)</Label>
                                    {renderResponse(selectedExecution)}
                                    <p className="text-xs text-(--secondary-text-wMain)">
                                        This is a truncated summary.{" "}
                                        <button
                                            className="underline hover:text-(--primary-text-wMain)"
                                            onClick={async () => {
                                                await handleSwitchSession(`scheduled_${selectedExecution.id}`);
                                                navigate("/chat");
                                            }}
                                        >
                                            Go to Chat
                                        </button>{" "}
                                        for the full response with inline artifacts.
                                    </p>
                                </div>

                                {/* Artifacts */}
                                {((selectedExecution.artifacts && selectedExecution.artifacts.length > 0) || (selectedExecution.resultSummary?.artifacts && selectedExecution.resultSummary.artifacts.length > 0)) && (
                                    <div className="space-y-2">
                                        <Label className="text-(--color-secondaryText-wMain)">Artifacts ({(selectedExecution.artifacts?.length || 0) + (selectedExecution.resultSummary?.artifacts?.length || 0)})</Label>
                                        {renderArtifacts(selectedExecution)}
                                    </div>
                                )}

                                {/* Task Configuration */}
                                <div className="space-y-4 border-t pt-4">
                                    <h3 className="text-sm font-semibold">Task Configuration</h3>

                                    <div className="space-y-2">
                                        <Label className="text-xs text-(--secondary-text-wMain)">Agent</Label>
                                        <div className="text-sm">{task.targetAgentName}</div>
                                    </div>

                                    <div className="space-y-2">
                                        <Label className="text-xs text-(--secondary-text-wMain)">Schedule</Label>
                                        <div className="text-sm">{formatScheduleExpression(task)}</div>
                                    </div>

                                    {task.taskMessage && task.taskMessage.length > 0 && (
                                        <div className="space-y-2">
                                            <Label className="text-xs text-(--secondary-text-wMain)">Message</Label>
                                            <div className="rounded bg-(--secondary-w10) p-3 text-sm break-words whitespace-pre-wrap">{task.taskMessage.map((part: { text?: string }) => part.text).join("\n")}</div>
                                        </div>
                                    )}
                                </div>
                            </div>
                        </div>
                    ) : (
                        <div className="flex h-full items-center justify-center">
                            <p className="text-(--secondary-text-wMain)">Select an execution to view details</p>
                        </div>
                    )}
                </div>

                {/* Right Panel - Artifact Preview (styled like Files tab) */}
                {previewArtifact && (
                    <div className="bg-background flex w-[450px] flex-shrink-0 flex-col">
                        {/* Header with back button (matching Files tab) */}
                        <div className="flex items-center gap-2 border-b p-2">
                            <Button variant="ghost" onClick={() => setPreviewArtifact(null)}>
                                <ArrowLeft />
                            </Button>
                            <div className="text-md font-semibold">Preview</div>
                        </div>

                        <div className="flex min-h-0 flex-1 flex-col gap-2">
                            {/* Artifact Details (matching ArtifactDetails component style) */}
                            <div className="border-b px-4 py-3">
                                <div className="flex flex-row justify-between gap-1">
                                    <div className="flex min-w-0 items-center gap-4">
                                        <div className="min-w-0">
                                            <div className="flex items-center gap-2">
                                                <div className="truncate text-sm" title={previewArtifact.name}>
                                                    {previewArtifact.name}
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                    <div className="whitespace-nowrap">
                                        <Button
                                            variant="ghost"
                                            size="sm"
                                            onClick={() => {
                                                try {
                                                    const resolved = resolveArtifactUri(previewArtifact.uri);
                                                    const url = new URL(resolved, window.location.origin);
                                                    if (url.origin !== window.location.origin) {
                                                        console.warn("Blocked external artifact URL:", previewArtifact.uri);
                                                        return;
                                                    }
                                                    window.open(`${url.href}?download=true`, "_blank");
                                                } catch {
                                                    // Malformed URI — silently ignore
                                                }
                                            }}
                                            tooltip="Download"
                                        >
                                            <Download />
                                        </Button>
                                    </div>
                                </div>
                            </div>

                            {/* Preview Content (matching ArtifactPanel structure) */}
                            <div className="min-h-0 min-w-0 flex-1 overflow-y-auto">
                                {loadingArtifact ? (
                                    <div className="flex h-full items-center justify-center">
                                        <div className="border-primary size-8 animate-spin rounded-full border-2 border-t-transparent" />
                                    </div>
                                ) : artifactContent ? (
                                    <div className="relative h-full w-full">
                                        {(() => {
                                            const mimeType = "text/plain";
                                            const rendererType = getRenderType(previewArtifact.name, mimeType);
                                            return rendererType ? (
                                                <ContentRenderer content={artifactContent} rendererType={rendererType} mime_type={mimeType} setRenderError={() => {}} />
                                            ) : (
                                                <pre className="p-4 text-sm break-words whitespace-pre-wrap">{artifactContent}</pre>
                                            );
                                        })()}
                                    </div>
                                ) : (
                                    <div className="flex h-full items-center justify-center">
                                        <p className="text-sm text-(--secondary-text-wMain)">No content available</p>
                                    </div>
                                )}
                            </div>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
};
