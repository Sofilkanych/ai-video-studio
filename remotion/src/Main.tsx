import { AbsoluteFill, Sequence, useCurrentFrame } from "remotion";
import { CaptionLayer } from "./components/CaptionLayer";
import { PresenterBubble } from "./components/PresenterBubble";
import { SlideScene } from "./components/SlideScene";
import type { MainProps, Scene } from "./types";

const DEFAULT_STYLE = { background: "#13202f", accent: "#f2a900", font: "Helvetica Neue, Arial, sans-serif" };

// Одна композиція на весь ролик. Геометрію обчислює Python (scripts/layout.py).
export const Main: React.FC<MainProps> = (props) => {
  const { timeline } = props;
  const style = { ...DEFAULT_STYLE, ...props.style };
  const frame = useCurrentFrame();
  const t = frame / timeline.fps;
  // сцена за кадрами — ті самі межі, що й у Sequence нижче
  const current: Scene =
    timeline.scenes.find(
      (s) => frame >= Math.round(s.start * timeline.fps) && frame < Math.round(s.end * timeline.fps),
    ) ?? timeline.scenes[timeline.scenes.length - 1];
  const geo = props.geometry?.[current.scene_id];

  return (
    <AbsoluteFill style={{ backgroundColor: style.background, fontFamily: style.font }}>
      {timeline.scenes.map((scene) => {
        const from = Math.round(scene.start * timeline.fps);
        const to = Math.round(scene.end * timeline.fps);
        const g = props.geometry?.[scene.scene_id];
        const slide = scene.slide != null ? props.slides?.[String(scene.slide)] : undefined;
        if (!g || !g.slide || !slide) return null;
        return (
          <Sequence key={scene.scene_id} from={from} durationInFrames={Math.max(1, to - from)} layout="none">
            <SlideScene
              scene={scene}
              rect={g.slide}
              slide={slide}
              elements={props.elements ?? {}}
              fps={timeline.fps}
              accent={style.accent}
            />
          </Sequence>
        );
      })}
      {props.presenter && geo?.presenter ? (
        <PresenterBubble
          presenter={props.presenter}
          rect={geo.presenter}
          shape={geo.presenter_shape ?? "circle"}
          fps={timeline.fps}
          fullFrame={current.layout === "presenter_full"}
        />
      ) : null}
      {timeline.captions?.enabled !== false && geo ? (
        <CaptionLayer captions={props.captions ?? []} rect={geo.caption} time={t} height={timeline.height} />
      ) : null}
    </AbsoluteFill>
  );
};
