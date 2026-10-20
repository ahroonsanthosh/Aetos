/**
 * The prompt an operator hands to a vision-capable AI (Claude, or any other) along with
 * photographs of a real building's walls, to get back JSON in exactly the shape
 * `facadeDesignSchema` (see `schema.ts`) accepts.
 *
 * Kept as one exported string rather than assembled from parts: the whole point is that it
 * produces the same, correctly-shaped answer every time, which means it has to say exactly
 * the same thing every time. This is the one copy of it; the editor's "Copy photo-analysis
 * prompt" button reads straight from here rather than keeping a second one that could drift.
 */
export const PHOTO_FACADE_PROMPT = `You are looking at photographs of the exterior walls of a single real building, each taken standing outside facing directly at one wall. For each photo, I will tell you which compass direction that wall faces (north, south, east, or west).

Your job: identify the position and approximate size of every window and door visible on each wall, and return them as JSON in the exact structure below. Do not invent, average, or "complete" a pattern, and do not fill in a wall I did not give you a photo of. If you cannot see a wall clearly, or aren't confident about part of it, leave that part out rather than guessing — an empty array is the correct answer when you're not sure, not a failure.

Coordinate system for each wall (read carefully, this is the part most likely to go wrong):
- Treat the wall as a flat rectangle, as if looking straight at it from outside.
- x = 0 is the LEFT edge of that wall as you face it; x = 1 is the RIGHT edge.
- y = 0 is the GROUND; y = 1 is the ROOFLINE (top of the wall).
- For each window or door, give its bounding box as x, y, width, height, where (x, y) is the BOTTOM-LEFT corner of the box, and width/height extend up and to the right from there.
- All four numbers are fractions of that wall's own width and height, between 0 and 1. Do not estimate metres.
- Doors normally start at y = 0.
- If a wall has a regular grid of windows, list every one individually rather than describing it as a grid.

Output ONLY a JSON object in exactly this shape — no other text before or after it:

{
  "walls": {
    "north": {
      "windows": [
        { "x": 0.08, "y": 0.32, "width": 0.09, "height": 0.16 },
        { "x": 0.24, "y": 0.32, "width": 0.09, "height": 0.16 }
      ],
      "doors": [
        { "x": 0.45, "y": 0, "width": 0.09, "height": 0.28 }
      ]
    }
  }
}

Only include a "north" / "south" / "east" / "west" key for a wall you were actually given a photo of and are reasonably confident about. If a wall has no doors, use an empty array for "doors" rather than omitting the key.

Here are the photos. For each one I'll say which direction that wall faces:
- Photo 1: this is the ______ wall
- Photo 2: this is the ______ wall
- Photo 3: this is the ______ wall
- Photo 4: this is the ______ wall`;
