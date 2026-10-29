"use client";

import {
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type ReactNode,
  type Ref,
} from "react";
import {
  Background,
  BaseEdge,
  EdgeLabelRenderer,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  getBezierPath,
  getSmoothStepPath,
  useUpdateNodeInternals,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeProps,
  type ReactFlowInstance,
} from "@xyflow/react";
import type { FlowConfig, FlowNode } from "../lib/apps";
import {
  branchLabel,
  branchesOf,
  replaceNode,
  setNext,
  typeLabels,
} from "../lib/flow-edit";
import {
  CARD_HEADING_HEIGHT,
  CARD_WIDTH,
  cardHeight,
  freePosition,
  initialPositions,
  layoutNodes,
  type Point,
} from "../lib/flow-layout";
import type { Pending } from "./flow-add-node";

export type AddPosition = { point?: Point; menu: Point; pending: Pending };
export type CanvasActions = {
  positionFor: (node: FlowNode, preferred?: Point) => Point;
  positionOf: (id: string) => Point;
  deleteSelection: () => boolean;
};
type CanvasNode = Node<{ node: FlowNode; invalid: boolean }, "flow">;
type CanvasEdge = Edge<
  {
    text: string;
    loop?: { top: number; bottom: number };
    remove: () => void;
    disabled: boolean;
  },
  "flow"
>;

function NodeCard({ data, isConnectable }: NodeProps<CanvasNode>) {
  const { node, invalid } = data;
  const updateNodeInternals = useUpdateNodeInternals();
  const branches = branchesOf(node);
  const handles = branches.join(",");
  useEffect(
    () => updateNodeInternals(node.id),
    [node.id, handles, updateNodeInternals],
  );
  return (
    <div
      className={`canvas-node-card${invalid ? " invalid" : ""}`}
      style={{ height: cardHeight(node) }}
    >
      {node.type !== "start" && (
        <Handle
          type="target"
          position={Position.Left}
          id="in"
          style={{ top: CARD_HEADING_HEIGHT / 2 }}
          isConnectable={isConnectable}
          data-testid={`canvas-in-${node.id}`}
          aria-label={`${node.title}的入口`}
        />
      )}
      <div className="canvas-node-heading">
        <span className={`node-type-badge ${node.type}`}>
          {typeLabels[node.type]}
        </span>
        <strong>{node.title}</strong>
        <code>
          {node.id}
          {invalid ? " · 有问题" : ""}
        </code>
      </div>
      {branches.map((branch) => (
        <div className="canvas-branch" key={branch}>
          <span>{branchLabel(node, branch)}</span>
          <Handle
            type="source"
            position={Position.Right}
            id={`out:${branch}`}
            isConnectable={isConnectable}
            data-testid={`canvas-out-${node.id}-${branch || "next"}`}
            aria-label={`${node.title}的${branchLabel(node, branch)}出口`}
          />
        </div>
      ))}
    </div>
  );
}

function BranchEdge(props: EdgeProps<CanvasEdge>) {
  const loop = props.data?.loop;
  const midpointY = (props.sourceY + props.targetY) / 2;
  const [path, x, y] = loop
    ? getSmoothStepPath({
        ...props,
        offset: 40,
        borderRadius: 12,
        centerY:
          midpointY - loop.top < loop.bottom - midpointY
            ? loop.top
            : loop.bottom,
      })
    : getBezierPath(props);
  return (
    <>
      <BaseEdge
        path={path}
        markerEnd={props.markerEnd}
        style={props.style}
        interactionWidth={24}
      />
      {(props.data?.text || props.selected) && (
        <EdgeLabelRenderer>
          <div
            className={`canvas-edge-label nodrag nopan${props.selected ? " selected" : ""}`}
            style={{
              transform: `translate(-50%, -50%) translate(${x}px, ${y}px)`,
            }}
          >
            {props.data?.text && <span>{props.data.text}</span>}
            {props.selected && (
              <button
                type="button"
                disabled={props.data?.disabled}
                aria-label="删除选中连线"
                onClick={(event) => {
                  event.stopPropagation();
                  props.data?.remove();
                }}
              >
                ×
              </button>
            )}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

const nodeTypes = { flow: NodeCard };
const edgeTypes = { flow: BranchEdge };
const edgeId = (from: string, branch: string) => `${from}:${branch}`;
// Reserve space below the graph so the minimap does not cover its outlets.
const FIT_OPTIONS = {
  padding: { top: "40px", right: "40px", bottom: "132px", left: "40px" },
  maxZoom: 1,
} as const;

export function FlowCanvas({
  ref,
  config,
  setConfig,
  selectedId,
  select,
  removeNode,
  invalid,
  disabled,
  canAdd,
  openAdd,
  menu,
}: {
  ref: Ref<CanvasActions>;
  config: FlowConfig;
  setConfig: (config: FlowConfig) => void;
  selectedId: string;
  select: (id: string) => void;
  removeNode: (id: string) => void;
  invalid: Set<string>;
  disabled: boolean;
  canAdd: boolean;
  openAdd: (position: AddPosition) => void;
  menu?: ReactNode;
}) {
  const stage = useRef<HTMLDivElement>(null);
  const [instance, setInstance] = useState<ReactFlowInstance<
    CanvasNode,
    CanvasEdge
  > | null>(null);
  const ids = config.graph.nodes.map((node) => node.id).join(",");
  const [fallback, setFallback] = useState(() => ({
    ids,
    positions: initialPositions(config),
  }));
  // Cache unsaved layout across graph edits; only allocate positions for new IDs.
  if (fallback.ids !== ids) {
    setFallback({
      ids,
      positions: initialPositions({
        ...config,
        graph: {
          ...config.graph,
          nodes: config.graph.nodes.map((node) => ({
            ...node,
            ui: node.ui ?? fallback.positions[node.id],
          })),
        },
      }),
    });
  }
  const [moving, setMoving] = useState<Record<string, Point>>({});
  const [selectedEdge, setSelectedEdge] = useState("");
  const positionOf = (id: string) =>
    moving[id] ??
    config.graph.nodes.find((node) => node.id === id)?.ui ??
    fallback.positions[id] ?? { x: 48, y: 48 };
  function center() {
    const rect = stage.current?.getBoundingClientRect();
    return rect && instance
      ? instance.screenToFlowPosition({
          x: rect.left + rect.width / 2,
          y: rect.top + rect.height / 2,
        })
      : { x: 48, y: 48 };
  }
  function clearEdge(from: string, branch: string) {
    setConfig(setNext(config, from, branch, ""));
    setSelectedEdge("");
  }
  useImperativeHandle(ref, () => ({
    positionFor: (node, preferred) => {
      const midpoint = center();
      return freePosition(
        preferred ?? {
          x: midpoint.x - CARD_WIDTH / 2,
          y: midpoint.y - cardHeight(node) / 2,
        },
        cardHeight(node),
        config.graph.nodes.map((other) => ({
          ...positionOf(other.id),
          height: cardHeight(other),
        })),
      );
    },
    positionOf,
    deleteSelection: () => {
      if (disabled) return false;
      const edge = config.graph.edges.find(
        (edge) => edgeId(edge.from, edge.branch ?? "") === selectedEdge,
      );
      if (selectedEdge && !edge) return false;
      if (edge) clearEdge(edge.from, edge.branch ?? "");
      else if (selectedId) removeNode(selectedId);
      else return false;
      return true;
    },
  }));

  const nodes: CanvasNode[] = config.graph.nodes.map((node) => ({
    id: node.id,
    type: "flow",
    position: positionOf(node.id),
    width: CARD_WIDTH,
    measured: { width: CARD_WIDTH, height: cardHeight(node) },
    data: { node, invalid: invalid.has(node.id) },
    selected: !selectedEdge && node.id === selectedId,
    ariaLabel: `${node.title}（${node.id}）${invalid.has(node.id) ? "，有问题" : ""}`,
    ariaRole: "button",
  }));
  const byId = new Map(config.graph.nodes.map((node) => [node.id, node]));
  const edges: CanvasEdge[] = config.graph.edges.map((edge) => {
    const branch = edge.branch ?? "";
    const id = edgeId(edge.from, branch);
    const source = positionOf(edge.from);
    const target = positionOf(edge.to);
    // Return edges take the nearer route above or below cards in their corridor.
    const corridor =
      target.x <= source.x
        ? config.graph.nodes
            .map((node) => ({ ...positionOf(node.id), height: cardHeight(node) }))
            .filter(
              (point) =>
                point.x <= source.x + CARD_WIDTH + 40 &&
                point.x + CARD_WIDTH >= target.x - 40,
            )
        : [];
    const loop = corridor.length
      ? {
          top: Math.min(...corridor.map((point) => point.y)) - 40,
          bottom: Math.max(...corridor.map((point) => point.y + point.height)) + 40,
        }
      : undefined;
    return {
      id,
      type: "flow",
      source: edge.from,
      target: edge.to,
      sourceHandle: `out:${branch}`,
      targetHandle: "in",
      selected: selectedEdge === id,
      ariaLabel: `${byId.get(edge.from)?.title ?? edge.from} → ${byId.get(edge.to)?.title ?? edge.to}，${branchLabel(byId.get(edge.from), branch)}`,
      ariaRole: "button",
      style: {
        stroke: selectedEdge === id ? "#7152a3" : "#9385ab",
        strokeWidth: 2,
        strokeDasharray: branch ? "6 4" : undefined,
      },
      markerEnd: { type: MarkerType.ArrowClosed, color: "#9385ab" },
      data: {
        text: branch ? branchLabel(byId.get(edge.from), branch) : "",
        loop,
        disabled,
        remove: () => clearEdge(edge.from, branch),
      },
    };
  });
  return (
    <div className="flow-canvas-editor">
      <div className="canvas-toolbar" role="toolbar" aria-label="画布工具">
        <button
          type="button"
          className="button secondary"
          disabled={disabled || !canAdd || !instance}
          onClick={() =>
            openAdd({ menu: { x: 16, y: 16 }, pending: null })
          }
        >
          添加节点
        </button>
        <button
          type="button"
          className="button secondary"
          disabled={disabled}
          onClick={() => {
            const positions = layoutNodes(config);
            setConfig({
              ...config,
              graph: {
                ...config.graph,
                nodes: config.graph.nodes.map((node) => ({
                  ...node,
                  ui: positions[node.id],
                })),
              },
            });
          }}
        >
          自动整理
        </button>
        <button
          type="button"
          className="text-button"
          aria-label="缩小"
          onClick={() => void instance?.zoomOut()}
        >
          −
        </button>
        <button
          type="button"
          className="text-button"
          aria-label="放大"
          onClick={() => void instance?.zoomIn()}
        >
          ＋
        </button>
        <button
          type="button"
          className="text-button"
          onClick={() => void instance?.fitView(FIT_OPTIONS)}
        >
          适应画布
        </button>
      </div>
      <div
        className="canvas-stage"
        ref={stage}
        role="group"
        aria-label="编排画布"
      >
        <ReactFlow<CanvasNode, CanvasEdge>
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          onInit={setInstance}
          fitView
          fitViewOptions={FIT_OPTIONS}
          minZoom={0.15}
          maxZoom={2}
          deleteKeyCode={null}
          multiSelectionKeyCode={null}
          nodesDraggable={!disabled}
          nodesConnectable={!disabled}
          edgesReconnectable={false}
          connectOnClick={false}
          onNodesChange={(changes) => {
            let updated = config;
            for (const change of changes) {
              if (change.type === "position" && change.position) {
                const point = change.position;
                if (change.dragging === undefined && !disabled) {
                  const node = byId.get(change.id);
                  if (node)
                    updated = replaceNode(updated, { ...node, ui: point });
                } else setMoving((value) => ({ ...value, [change.id]: point }));
              }
              if (change.type === "select" && change.selected) {
                setSelectedEdge("");
                select(change.id);
              }
            }
            if (updated !== config) setConfig(updated);
          }}
          onEdgesChange={(changes) => {
            for (const change of changes)
              if (change.type === "select" && change.selected)
                setSelectedEdge(change.id);
          }}
          onNodeClick={(_, node) => {
            setSelectedEdge("");
            select(node.id);
          }}
          onPaneClick={() => {
            setSelectedEdge("");
            select("");
          }}
          onEdgeClick={(_, edge) => setSelectedEdge(edge.id)}
          onNodeDragStop={(_, dragged) => {
            const node = byId.get(dragged.id);
            if (node && !disabled)
              setConfig(replaceNode(config, { ...node, ui: dragged.position }));
            setMoving((value) => {
              const next = { ...value };
              delete next[dragged.id];
              return next;
            });
          }}
          isValidConnection={(connection) =>
            byId.get(connection.target)?.type !== "start" &&
            Boolean(connection.sourceHandle?.startsWith("out:"))
          }
          onConnect={(connection) => {
            if (!disabled && byId.get(connection.target)?.type !== "start")
              setConfig(
                setNext(
                  config,
                  connection.source,
                  connection.sourceHandle?.slice(4) ?? "",
                  connection.target,
                ),
              );
          }}
          onConnectEnd={(event, state) => {
            if (
              disabled ||
              state.isValid ||
              state.fromHandle?.type !== "source" ||
              !instance
            )
              return;
            const target = event.target;
            if (!(target instanceof Element)) return;
            const targetId = target
              .closest(".react-flow__node")
              ?.getAttribute("data-id");
            if (targetId) {
              if (byId.get(targetId)?.type !== "start")
                setConfig(
                  setNext(
                    config,
                    state.fromNode!.id,
                    state.fromHandle.id?.slice(4) ?? "",
                    targetId,
                  ),
                );
              return;
            }
            if (state.toNode || !canAdd || !target.closest(".react-flow__pane"))
              return;
            const cursor =
              "changedTouches" in event ? event.changedTouches[0] : event;
            const rect = stage.current!.getBoundingClientRect();
            const point = instance.screenToFlowPosition({
              x: cursor.clientX,
              y: cursor.clientY,
            });
            openAdd({
              point: { x: point.x, y: point.y - CARD_HEADING_HEIGHT / 2 },
              menu: {
                x: Math.max(
                  8,
                  Math.min(cursor.clientX - rect.left, rect.width - 332),
                ),
                y: Math.max(
                  8,
                  Math.min(cursor.clientY - rect.top, rect.height - 360),
                ),
              },
              pending: {
                from: state.fromNode!.id,
                branch: state.fromHandle.id?.slice(4) ?? "",
              },
            });
          }}
          ariaLabelConfig={{
            "node.a11yDescription.default":
              "按 Enter 打开节点设置，按 Delete 删除节点。",
            "edge.a11yDescription.default": "按 Delete 删除连线。",
            "minimap.ariaLabel": "画布小地图",
          }}
        >
          <Background gap={20} color="#ded8e8" />
          <MiniMap
            position="bottom-right"
            pannable
            zoomable
            nodeColor={(node) => (node.data.invalid ? "#b5533c" : "#b2a1d3")}
          />
        </ReactFlow>
        {menu}
      </div>
    </div>
  );
}
