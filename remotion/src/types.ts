// Типи відповідають schemas/timeline.schema.json і props, які будує scripts/assets.py.

export type Rect = [number, number, number, number]; // x, y, w, h

export type PresenterPosition = "bottom-right" | "bottom-left" | "top-right" | "top-left";

export type Action = {
  at: number;
  until?: number;
  type: "zoom" | "highlight" | "callout" | "table_reveal";
  target: string;
  text?: string;
};

export type Scene = {
  scene_id: string;
  start: number;
  end: number;
  slide?: number | null;
  layout: "slide_inset" | "slide_full" | "presenter_full" | "side_by_side" | "free_zone";
  presenter?: { shape: "circle" | "rect"; position: PresenterPosition; scale: number } | null;
  transition_in?: "cut" | "fade" | "slide" | "zoom";
  actions?: Action[];
  generated_assets?: string[];
  sync_confidence?: number;
};

export type Timeline = {
  version: string;
  fps: number;
  width: number;
  height: number;
  duration: number;
  captions?: { enabled?: boolean; position?: "bottom" | "top"; scale?: number };
  scenes: Scene[];
};

export type Geometry = {
  slide: Rect | null;
  presenter: Rect | null;
  presenter_shape: "circle" | "rect" | null;
  caption: Rect;
};

export type SlideAsset = { src: string; w: number; h: number; bg: string };

export type ElementInfo = {
  slide: number;
  type: string;
  bbox: Rect; // px PNG слайда
  rows?: [number, number][]; // для таблиць: [y, h] кожного рядка, px PNG
};

export type Caption = { start: number; end: number; text: string };

export type MainProps = {
  timeline: Timeline;
  geometry?: Record<string, Geometry>;
  slides?: Record<string, SlideAsset>;
  elements?: Record<string, ElementInfo>;
  presenter?: { src: string; focus: { x: number; y: number }; duration: number } | null;
  captions?: Caption[];
  style?: { background: string; accent: string; font: string };
};
