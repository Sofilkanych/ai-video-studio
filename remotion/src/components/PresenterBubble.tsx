import { Freeze, OffthreadVideo, staticFile, useCurrentFrame } from "remotion";
import type { Rect } from "../types";

type Props = {
  presenter: { src: string; focus: { x: number; y: number }; duration: number };
  rect: Rect;
  shape: "circle" | "rect";
  fps: number;
  fullFrame: boolean;
};

// Одне безперервне відео ведучого на весь ролик (синхронно з master-аудіо), змінюється лише рамка.
// Якщо відео коротше за аудіо (на частки секунди), останній кадр «заморожується», а не зникає.
export const PresenterBubble: React.FC<Props> = ({ presenter, rect, shape, fps, fullFrame }) => {
  const [x, y, w, h] = rect;
  const frame = useCurrentFrame();
  const lastFrame = Math.max(0, Math.floor(presenter.duration * fps) - 2);
  const video = (
    <OffthreadVideo
      src={staticFile(presenter.src)}
      muted
      style={{
        width: "100%",
        height: "100%",
        objectFit: "cover",
        objectPosition: `${presenter.focus.x * 100}% ${presenter.focus.y * 100}%`,
      }}
    />
  );
  return (
      <div
        style={{
          position: "absolute",
          left: x,
          top: y,
          width: w,
          height: h,
          overflow: "hidden",
          borderRadius: fullFrame ? 0 : shape === "circle" ? "50%" : 12,
          border: fullFrame ? "none" : `${Math.max(3, h * 0.02)}px solid rgba(255,255,255,0.9)`,
          boxShadow: fullFrame ? "none" : "0 8px 24px rgba(0,0,0,0.35)",
          boxSizing: "border-box",
          background: "#000",
        }}
      >
        {frame > lastFrame ? <Freeze frame={lastFrame}>{video}</Freeze> : video}
      </div>
  );
};
