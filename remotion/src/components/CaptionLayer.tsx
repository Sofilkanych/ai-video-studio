import type { Caption, Rect } from "../types";

type Props = { captions: Caption[]; rect: Rect; time: number; height: number };

// Субтитри в межах caption-прямокутника сцени (Python гарантує, що він не перекриває вміст слайда
// у slide_inset; для slide_full перекриття перевіряє геометричний QA).
export const CaptionLayer: React.FC<Props> = ({ captions, rect, time, height }) => {
  const cap = captions.find((c) => time >= c.start && time < c.end);
  if (!cap) return null;
  const [x, y, w, h] = rect;
  const fontSize = Math.min(height * 0.037, h / 2.6);
  return (
    <div
      style={{
        position: "absolute",
        left: x,
        top: y,
        width: w,
        height: h,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          maxWidth: "100%",
          padding: `${fontSize * 0.15}px ${fontSize * 0.5}px`,
          background: "rgba(0,0,0,0.55)",
          borderRadius: fontSize * 0.25,
          color: "white",
          fontSize,
          lineHeight: 1.2,
          textAlign: "center",
          whiteSpace: "pre-line",
          fontWeight: 500,
        }}
      >
        {cap.text}
      </div>
    </div>
  );
};
