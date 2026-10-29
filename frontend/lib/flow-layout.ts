import { graphlib, layout } from "@dagrejs/dagre";
import type { FlowConfig, FlowNode } from "./apps";
import { branchesOf } from "./flow-edit";

export type Point = { x: number; y: number };
export const CARD_WIDTH = 232;
export const cardHeight = (node: FlowNode) => 88 + branchesOf(node).length * 28;

// Layout is a view concern. Callers explicitly decide when to save these points.
export function layoutNodes(config: FlowConfig): Record<string, Point> {
  const graph = new graphlib.Graph({ multigraph: true });
  graph.setGraph({
    rankdir: "TB",
    nodesep: 64,
    ranksep: 100,
    marginx: 48,
    marginy: 48,
  });
  graph.setDefaultEdgeLabel(() => ({}));
  for (const node of config.graph.nodes)
    graph.setNode(node.id, { width: CARD_WIDTH, height: cardHeight(node) });
  config.graph.edges.forEach((edge, i) => {
    if (graph.hasNode(edge.from) && graph.hasNode(edge.to))
      graph.setEdge(edge.from, edge.to, {}, String(i));
  });
  layout(graph);
  return Object.fromEntries(
    config.graph.nodes.map((node) => {
      const point = graph.node(node.id);
      return [
        node.id,
        { x: point.x - CARD_WIDTH / 2, y: point.y - cardHeight(node) / 2 },
      ];
    }),
  );
}

// Keep saved positions fixed, including in partially positioned imports.
export function initialPositions(config: FlowConfig): Record<string, Point> {
  const positions = layoutNodes(config);
  const occupied = config.graph.nodes
    .filter((node) => node.ui)
    .map((node) => ({
      ...node.ui!,
      height: cardHeight(node),
    }));
  for (const node of config.graph.nodes) {
    if (node.ui) {
      positions[node.id] = node.ui;
      continue;
    }
    const point = positions[node.id];
    while (
      occupied.some(
        (other) =>
          point.x < other.x + CARD_WIDTH + 24 &&
          point.x + CARD_WIDTH + 24 > other.x &&
          point.y < other.y + other.height + 24 &&
          point.y + cardHeight(node) + 24 > other.y,
      )
    )
      point.y += 48;
    occupied.push({ ...point, height: cardHeight(node) });
  }
  return positions;
}
