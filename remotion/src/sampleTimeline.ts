import type { Timeline } from "./types";

// Тестовий timeline для перевірки рендеру у Фазі 0 (без медіа).
export const sampleTimeline: Timeline = {
  version: "v001",
  fps: 30,
  width: 1280,
  height: 720,
  duration: 6,
  scenes: [
    { scene_id: "s1", start: 0, end: 2, slide: 1, layout: "slide_inset" },
    { scene_id: "s2", start: 2, end: 4, slide: 2, layout: "slide_inset" },
    { scene_id: "s3", start: 4, end: 6, slide: 3, layout: "slide_inset" },
  ],
};
