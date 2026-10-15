import type { Envelope, Evidence } from "./types";

export type Layer = "sanctioned" | "merchant" | "account" | "identity" | "org" | "governance" | "action";

export type GNode = {
  id: string;
  labels: string[];
  name: string | null;
  layer: Layer;
  fx?: number;
  fy?: number;
  labelAbove?: boolean;
};
export type GLink = { source: string; target: string; rel: string };
export type GraphData = { nodes: GNode[]; links: GLink[] };

export const LAYERS: Record<Layer, { color: string; label: string }> = {
  account: { color: "#60a5fa", label: "Account" },
  merchant: { color: "#a78bfa", label: "Merchant" },
  sanctioned: { color: "#f43f5e", label: "Sanctioned" },
  identity: { color: "#fbbf24", label: "Device / IP" },
  org: { color: "#34d399", label: "Employee / team" },
  governance: { color: "#94a3b8", label: "Decision / policy / source" },
  action: { color: "#f472b6", label: "Case / approval" },
};

export const MONEY = new Set(["TRANSFERRED", "FLOWS_TO"]);
export const IDENTITY = new Set(["USED_DEVICE", "LOGGED_FROM"]);

const PREFIX_LAYER: Record<string, Layer> = {
  acct: "account",
  merch: "merchant",
  dev: "identity",
  ip: "identity",
  emp: "org",
  team: "org",
  apr: "action",
  case: "action",
};

export function layerOf(labels: string[], id: string): Layer {
  const has = (label: string) => labels.includes(label);
  if (has("Sanctioned")) return "sanctioned";
  if (has("Merchant")) return "merchant";
  if (has("Account")) return "account";
  if (has("Device") || has("IP")) return "identity";
  if (has("Employee") || has("Team")) return "org";
  if (has("ApprovalRequest") || has("Case")) return "action";
  if (labels.length) return "governance";
  return PREFIX_LAYER[id.split("-")[0]] ?? "governance";
}

/** Accumulates tool evidence into one graph, keeping node objects stable so the layout doesn't jump. */
export class GraphStore {
  private nodes = new Map<string, GNode>();
  private links = new Map<string, GLink>();

  add(evidence: Evidence | undefined): boolean {
    if (!evidence) return false;
    let changed = false;
    const ensure = (id: string, labels: string[] = [], name: string | null = null) => {
      const node = this.nodes.get(id);
      if (!node) {
        this.nodes.set(id, { id, labels, name, layer: layerOf(labels, id) });
        changed = true;
        return;
      }
      if (!node.labels.length && labels.length) {
        node.labels = labels;
        node.layer = layerOf(labels, id);
        changed = true;
      }
      if (!node.name && name) {
        node.name = name;
        changed = true;
      }
    };
    for (const [id, info] of Object.entries(evidence.nodes)) ensure(id, info.labels, info.name);
    for (const [src, rel, dst] of evidence.edges) {
      ensure(src);
      ensure(dst);
      const key = `${src}|${MONEY.has(rel) ? "MONEY" : rel}|${dst}`;
      if (!this.links.has(key)) {
        this.links.set(key, { source: src, target: dst, rel });
        changed = true;
      }
    }
    return changed;
  }

  data(): GraphData {
    return { nodes: [...this.nodes.values()], links: [...this.links.values()] };
  }

  get size() {
    return { nodes: this.nodes.size, links: this.links.size };
  }
}

export function graphOf(evidence: Evidence | undefined): GraphData {
  const store = new GraphStore();
  store.add(evidence);
  return store.data();
}

/** Pins each path into a left-to-right row (longest first) so a multi-hop chain reads in order. */
export function pinChains(data: GraphData, chains: string[][], gap = 90): GraphData {
  const byId = new Map(data.nodes.map((node) => [node.id, node]));
  const placed = new Set<string>();
  [...chains]
    .sort((a, b) => b.length - a.length)
    .forEach((chain, row) => {
      chain.forEach((id, i) => {
        const node = byId.get(id);
        if (!node || placed.has(id)) return;
        node.fx = (i - (chain.length - 1) / 2) * gap;
        node.fy = row * 70;
        node.labelAbove = i % 2 === 1;
        placed.add(id);
      });
    });
  return data;
}

type FlaggedHit = { entity: { labels: string[] }; path: { nodes: string[] } };

export function flaggedChains(envelopes: (Envelope | undefined)[]): string[][] {
  return envelopes.flatMap((envelope) =>
    envelope?.ok && envelope.tool === "explore_neighborhood"
      ? (envelope.data.flagged as FlaggedHit[]).map((hit) => hit.path.nodes)
      : [],
  );
}
