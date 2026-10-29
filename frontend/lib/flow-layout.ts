import { graphlib, layout } from "@dagrejs/dagre";
import type { FlowConfig, FlowNode } from "./apps";
import { branchesOf } from "./flow-edit";

export type Point = { x: number; y: number };
export const CARD_WIDTH = 232;
export const CARD_HEADING_HEIGHT = 112;
export const cardHeight = (node: FlowNode) =>
  CARD_HEADING_HEIGHT + branchesOf(node).length * 28;

export function freePosition(
  preferred: Point,
  height: number,
  occupied: (Point & { height: number })[],
): Point {
  const point = { ...preferred };
  for (;;) {
    const overlaps = occupied.filter(
      (other) =>
        point.x < other.x + CARD_WIDTH + 24 &&
        point.x + CARD_WIDTH + 24 > other.x &&
        point.y < other.y + other.height + 24 &&
        point.y + height + 24 > other.y,
    );
    if (!overlaps.length) return point;
    point.y = Math.max(...overlaps.map((other) => other.y + other.height + 40));
  }
}

// Layout is a view concern. Callers explicitly decide when to save these points.
export function layoutNodes(config: FlowConfig): Record<string, Point> {
  const graph = new graphlib.Graph({ multigraph: true });
  graph.setGraph({
    rankdir: "LR",
    nodesep: 40,
    ranksep: 120,
    marginx: 48,
    marginy: 48,
  });
  graph.setDefaultEdgeLabel(() => ({}));
  for (const node of config.graph.nodes)
    graph.setNode(node.id, { width: CARD_WIDTH, height: cardHeight(node) });
  let edgeIndex = 0;
  for (const node of config.graph.nodes) {
    const branches = branchesOf(node);
    // Dagre uses insertion order to break ties between sibling nodes.
    const edges = config.graph.edges
      .filter((edge) => edge.from === node.id)
      .sort(
        (a, b) =>
          branches.indexOf(a.branch ?? "") - branches.indexOf(b.branch ?? ""),
      );
    for (const edge of edges)
      if (graph.hasNode(edge.to))
        graph.setEdge(edge.from, edge.to, {}, String(edgeIndex++));
  }
  // Dagre's crossing heuristic assumes one outlet and can reverse branch rows.
  layout(graph, { disableOptimalOrderHeuristic: true });
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
    const point = freePosition(positions[node.id], cardHeight(node), occupied);
    positions[node.id] = point;
    occupied.push({ ...point, height: cardHeight(node) });
  }
  return positions;
}
