// Follow /api/v1/stream with EventSource. EventSource reconnects by itself and sends
// Last-Event-ID, so every event missed while disconnected is replayed.
//
// Browser: paste into a page served by the app (same origin; no API keys set).
// Node 18+: npm install eventsource@2 && node examples/stream.js
//   DISCOVER_URL=http://nas:8000 DISCOVER_API_KEY=... node examples/stream.js
// (Browsers cannot send headers with EventSource; with DISCOVER_API_KEYS set, use Node or
// a proxy that adds the Authorization header.)

const base = (typeof process !== "undefined" && process.env.DISCOVER_URL) || "";
const key = typeof process !== "undefined" ? process.env.DISCOVER_API_KEY : undefined;
const ES = typeof EventSource !== "undefined" ? EventSource : require("eventsource");

const url = `${base}/api/v1/stream?profile=personal-reader&include=card`;
const source = new ES(url, key ? { headers: { Authorization: `Bearer ${key}` } } : undefined);

source.addEventListener("hello", (e) => {
  const hello = JSON.parse(e.data);
  console.log(`connected; latest event ${hello.latest_id}`);
});
for (const type of ["story.published", "story.updated", "story.retracted"]) {
  source.addEventListener(type, (e) => {
    const event = JSON.parse(e.data);
    const title = event.story ? event.story.title : "";
    console.log(`event ${e.lastEventId} ${type} story ${event.story_id} v${event.version} ${title}`);
  });
}
source.onerror = () => console.log("disconnected; EventSource will resume with Last-Event-ID");
