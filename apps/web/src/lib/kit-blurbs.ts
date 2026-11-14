/**
 * Plain-language descriptions of the built-in Kits, written for someone who has never heard of the tools behind them.
 * The catalogue API only sends a name and a category; the manifest descriptions are written for engineers. A Kit that
 * is not listed here simply shows no blurb (nothing is invented for it).
 */
export interface KitBlurb {
  /** One sentence: what the Kit does. */
  does: string;
  /** What the person provides. Omitted for always-on services and sessions. */
  input?: string;
  /** What the person gets back. */
  output?: string;
}

const BLURBS: Record<string, KitBlurb> = {
  upscale: {
    does: "Make pictures bigger and sharper.",
    input: "A picture, or a .zip of pictures",
    output: "Bigger, sharper pictures",
  },
  "ai-image": {
    does: "Create pictures from a written description.",
    input: "A text description",
    output: "New images",
  },
  "ai-video": {
    does: "Create a short video from a written description.",
    input: "A text description",
    output: "A video",
  },
  "comfyui-workflow-runner": {
    does: "Run a ComfyUI image workflow without installing anything.",
    input: "Your ComfyUI workflow file",
    output: "The images it makes",
  },
  "3d-gaussian-splat-train": {
    does: "Turn a set of photos into a 3D scene.",
    input: "A set of photos of one place or object",
    output: "A trained 3D scene",
  },
  "ffmpeg-transcode": {
    does: "Convert a video to another format.",
    input: "A video file",
    output: "The converted video",
  },
  "video-upscale-frames": {
    does: "Make video frames bigger and sharper, frame by frame.",
    input: "A .zip of numbered frames",
    output: "Sharper frames",
  },
  "blender-render": {
    does: "Render a 3D scene or animation from your Blender file.",
    input: "A .blend file",
    output: "The rendered images",
  },
  "whisper-transcribe": {
    does: "Turn speech in an audio or video file into written text.",
    input: "An audio or video file",
    output: "A written transcript",
  },
  "text-to-speech": {
    does: "Turn written text into spoken audio.",
    input: "Your text",
    output: "Audio files",
  },
  "batch-embeddings": {
    does: "Turn a list of texts into numbers that a program can compare.",
    input: "A list of texts",
    output: "A table of numbers",
  },
  "llm-finetune-lora": {
    does: "Teach an open AI model from your own examples.",
    input: "Your training examples (a JSONL file)",
    output: "A trained add-on for the model",
  },
  "llm-serve": {
    does: "Run your own AI model behind a web address that other programs can call.",
  },
  notebooks: {
    does: "Open a notebook in your browser on a computer with a graphics card.",
  },
  "scientific-compute": {
    does: "Run your own Python calculation script. No graphics card is needed.",
    input: "A Python script",
    output: "Your result files",
  },
};

export function kitBlurb(slug: string): KitBlurb | null {
  return Object.prototype.hasOwnProperty.call(BLURBS, slug) ? BLURBS[slug] : null;
}
