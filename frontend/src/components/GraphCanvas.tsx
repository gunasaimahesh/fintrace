import { useEffect, useRef, useState } from "react";
import ForceGraph2D, { type ForceGraphMethods, type LinkObject, type NodeObject } from "react-force-graph-2d";
import { IDENTITY, LAYERS, MONEY, type GLink, type GNode, type GraphData } from "../graph";

type Node = NodeObject<GNode>;
type Link = LinkObject<GNode, GLink>;

const MAX_ZOOM = 2.2;

function linkColor(link: Link) {
  if (MONEY.has(link.rel)) return "rgba(96, 165, 250, 0.75)";
  if (IDENTITY.has(link.rel)) return "rgba(251, 191, 36, 0.85)";
  return "rgba(148, 163, 184, 0.45)";
}

export function GraphCanvas({ data, focusId }: { data: GraphData; focusId?: string }) {
  const wrapper = useRef<HTMLDivElement>(null);
  const graph = useRef<ForceGraphMethods<Node, Link> | undefined>(undefined);
  const [size, setSize] = useState({ width: 0, height: 0 });

  useEffect(() => {
    const element = wrapper.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) =>
      setSize({ width: entry.contentRect.width, height: entry.contentRect.height }),
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const fit = () => {
    const instance = graph.current;
    if (!instance) return;
    instance.zoomToFit(400, 80);
    setTimeout(() => instance.zoom() > MAX_ZOOM && instance.zoom(MAX_ZOOM, 300), 450);
  };

  useEffect(() => {
    const instance = graph.current;
    if (!instance) return;
    (instance.d3Force("charge") as unknown as { strength: (s: number) => void } | undefined)?.strength(-160);
    (instance.d3Force("link") as unknown as { distance: (d: number) => void } | undefined)?.distance(55);
  }, [size.width > 0]);

  useEffect(() => {
    const timer = setTimeout(fit, 700);
    return () => clearTimeout(timer);
  }, [data]);

  const drawNode = (node: Node, ctx: CanvasRenderingContext2D, scale: number) => {
    const x = node.x ?? 0;
    const y = node.y ?? 0;
    const focus = node.id === focusId;
    const radius = focus ? 8 : node.layer === "sanctioned" ? 7 : 5;
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, 2 * Math.PI);
    ctx.fillStyle = LAYERS[node.layer].color;
    ctx.fill();
    if (focus || node.layer === "sanctioned") {
      ctx.lineWidth = 2 / scale;
      ctx.strokeStyle = focus ? "#fafafa" : "#fecdd3";
      ctx.stroke();
    }
    if (scale < 0.7 && !focus && node.layer !== "sanctioned") return;
    ctx.font = `${Math.max(11 / scale, 2.5)}px ui-sans-serif, system-ui`;
    ctx.textAlign = "center";
    ctx.textBaseline = node.labelAbove ? "bottom" : "top";
    ctx.fillStyle = "rgba(228, 228, 231, 0.9)";
    const offset = radius + 2 / scale;
    ctx.fillText(node.id, x, node.labelAbove ? y - offset : y + offset);
  };

  return (
    <div ref={wrapper} className="absolute inset-0">
      {size.width > 0 && (
        <ForceGraph2D<GNode, GLink>
          ref={graph}
          width={size.width}
          height={size.height}
          graphData={data}
          backgroundColor="rgba(0,0,0,0)"
          cooldownTicks={150}
          d3VelocityDecay={0.3}
          onEngineStop={fit}
          nodeLabel={(node) => `${node.name ?? node.id} · ${node.labels.join(", ") || "entity"}`}
          nodeCanvasObject={drawNode}
          nodePointerAreaPaint={(node, color, ctx) => {
            ctx.fillStyle = color;
            ctx.beginPath();
            ctx.arc(node.x ?? 0, node.y ?? 0, 8, 0, 2 * Math.PI);
            ctx.fill();
          }}
          linkColor={linkColor}
          linkWidth={(link) => (MONEY.has(link.rel) ? 1.8 : 1)}
          linkLineDash={(link) => (IDENTITY.has(link.rel) ? [4, 3] : null)}
          linkLabel={(link) => link.rel}
          linkDirectionalArrowLength={(link) => (IDENTITY.has(link.rel) ? 0 : 5)}
          linkDirectionalArrowRelPos={0.92}
          linkDirectionalParticles={(link) => (MONEY.has(link.rel) ? 2 : 0)}
          linkDirectionalParticleWidth={2.2}
          linkDirectionalParticleSpeed={0.006}
        />
      )}
    </div>
  );
}
