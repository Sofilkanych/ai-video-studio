import { Easing, Img, interpolate, staticFile, useCurrentFrame } from "remotion";
import type { Action, ElementInfo, Rect, Scene, SlideAsset } from "../types";

const FADE_FRAMES = 10;
const ZOOM_EASE = 0.6; // с

type Props = {
  scene: Scene;
  rect: Rect;
  slide: SlideAsset;
  elements: Record<string, ElementInfo>;
  fps: number;
  accent: string;
};

// Плавна поява/зникнення в межах [at, until]
const envelope = (t: number, at: number, until: number, ease: number) =>
  interpolate(t, [at, at + ease, Math.max(at + ease, until - ease), until], [0, 1, 1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.inOut(Easing.cubic),
  });

export const SlideScene: React.FC<Props> = ({ scene, rect, slide, elements, fps, accent }) => {
  const frame = useCurrentFrame(); // локальний кадр сцени
  const t = scene.start + frame / fps; // абсолютний час
  const [x, y, w, h] = rect;
  const sx = w / slide.w; // px PNG → px кадру
  const sy = h / slide.h;
  const actions = scene.actions ?? [];

  const opacity =
    scene.transition_in === "fade"
      ? interpolate(frame, [0, FADE_FRAMES], [0, 1], { extrapolateRight: "clamp" })
      : 1;

  // Zoom: активна дія zoom задає масштаб і зсув
  let scale = 1;
  let tx = 0;
  let ty = 0;
  for (const a of actions) {
    if (a.type !== "zoom") continue;
    const el = elements[a.target];
    if (!el) continue;
    const k = envelope(t, a.at, a.until ?? scene.end, ZOOM_EASE);
    if (k <= 0) continue;
    const [bx, by, bw, bh] = el.bbox;
    const target = Math.min(2.5, Math.max(1, Math.min((0.85 * slide.w) / bw, (0.85 * slide.h) / bh)));
    const s = 1 + (target - 1) * k;
    const cx = (bx + bw / 2) * sx;
    const cy = (by + bh / 2) * sy;
    // центр цілі — у центр рамки, не виходячи за краї слайда
    const maxTx = (s - 1) * w;
    const maxTy = (s - 1) * h;
    scale = s;
    tx = Math.min(0, Math.max(-maxTx, w / 2 - cx * s)) * 1;
    ty = Math.min(0, Math.max(-maxTy, h / 2 - cy * s)) * 1;
  }

  return (
    <div style={{ position: "absolute", left: x, top: y, width: w, height: h, overflow: "hidden", opacity }}>
      <div
        style={{
          position: "absolute",
          inset: 0,
          transformOrigin: "0 0",
          transform: `translate(${tx}px, ${ty}px) scale(${scale})`,
        }}
      >
        <Img src={staticFile(slide.src)} style={{ width: w, height: h, display: "block" }} />
        {actions.map((a, i) => (
          <Overlay key={i} action={a} t={t} sceneEnd={scene.end} el={elements[a.target]} sx={sx} sy={sy}
            slide={slide} accent={accent} frameH={h} />
        ))}
      </div>
    </div>
  );
};

const Overlay: React.FC<{
  action: Action;
  t: number;
  sceneEnd: number;
  el?: ElementInfo;
  sx: number;
  sy: number;
  slide: SlideAsset;
  accent: string;
  frameH: number;
}> = ({ action, t, sceneEnd, el, sx, sy, slide, accent, frameH }) => {
  if (!el) return null;
  const until = action.until ?? sceneEnd;
  const [bx, by, bw, bh] = el.bbox;
  const box = { left: bx * sx, top: by * sy, width: bw * sx, height: bh * sy };

  if (action.type === "highlight") {
    const k = envelope(t, action.at, until, 0.3);
    if (k <= 0) return null;
    // текст у PowerPoint буває ширшим за свій блок — рамка йде з відступом назовні,
    // у межах слайда, і не налазить на літери
    const border = Math.max(3, frameH * 0.005);
    const pad = Math.max(6, frameH * 0.012) + border;
    const slideW = slide.w * sx;
    const slideH = slide.h * sy;
    const left = Math.max(0, box.left - pad);
    const top = Math.max(0, box.top - pad);
    const right = Math.min(slideW, box.left + box.width + pad);
    const bottom = Math.min(slideH, box.top + box.height + pad);
    return (
      <div
        style={{
          position: "absolute",
          left,
          top,
          width: right - left,
          height: bottom - top,
          boxSizing: "border-box",
          border: `${border}px solid ${accent}`,
          borderRadius: 8,
          boxShadow: `0 0 0 9999px rgba(0,0,0,${0.35 * k})`,
          opacity: k,
        }}
      />
    );
  }

  if (action.type === "callout" && action.text) {
    const k = envelope(t, action.at, until, 0.3);
    if (k <= 0) return null;
    const fontSize = frameH * 0.04;
    const below = box.top + box.height + fontSize * 2.4 < slide.h * sy;
    return (
      <div
        style={{
          position: "absolute",
          left: Math.min(box.left, slide.w * sx * 0.55),
          top: below ? box.top + box.height + fontSize * 0.4 : Math.max(0, box.top - fontSize * 2),
          maxWidth: slide.w * sx * 0.45,
          padding: `${fontSize * 0.3}px ${fontSize * 0.6}px`,
          background: accent,
          color: "#111",
          fontSize,
          fontWeight: 600,
          borderRadius: 6,
          opacity: k,
          transform: `translateY(${(1 - k) * fontSize * 0.5}px)`,
        }}
      >
        {action.text}
      </div>
    );
  }

  if (action.type === "table_reveal" && el.rows && el.rows.length > 1) {
    // рядок 0 (заголовок) видно одразу; решта відкриваються рівномірно між at і until
    const n = el.rows.length - 1;
    const step = (until - action.at) / Math.max(1, n);
    return (
      <>
        {el.rows.slice(1).map(([ry, rh], i) => {
          const revealAt = action.at + i * step;
          const k = interpolate(t, [revealAt, revealAt + 0.4], [1, 0], {
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
          });
          if (k <= 0) return null;
          return (
            <div
              key={i}
              style={{
                position: "absolute",
                left: box.left - 2,
                top: ry * sy - 1,
                width: box.width + 4,
                height: rh * sy + 2,
                background: slide.bg,
                opacity: k,
              }}
            />
          );
        })}
      </>
    );
  }
  return null;
};
