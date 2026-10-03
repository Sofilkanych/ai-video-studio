import { Composition } from "remotion";
import { Main } from "./Main";
import { sampleTimeline } from "./sampleTimeline";
import type { MainProps } from "./types";

// Одна композиція на весь ролик. Розміри й тривалість беруться з timeline,
// який run.py передає через --props.
export const RemotionRoot: React.FC = () => (
  <Composition
    id="Main"
    component={Main}
    defaultProps={{ timeline: sampleTimeline } satisfies MainProps}
    calculateMetadata={({ props }) => ({
      fps: props.timeline.fps,
      width: props.timeline.width,
      height: props.timeline.height,
      durationInFrames: Math.round(props.timeline.duration * props.timeline.fps),
    })}
  />
);
