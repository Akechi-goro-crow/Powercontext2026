import cors from "cors";
import express from "express";
import { FulltextIndexConfig, Schema, SeekdbClient, VectorIndexConfig } from "seekdb";
import { fileURLToPath } from "node:url";
import path from "node:path";

const app = express();
const port = Number(process.env.PORT || 8791);
const database = process.env.SEEKDB_DATABASE || "stepfun_kb";
const host = process.env.SEEKDB_HOST || "127.0.0.1";
const seekdbPort = Number(process.env.SEEKDB_PORT || 2881);
const user = process.env.SEEKDB_USER || "root";
const password = process.env.SEEKDB_PASSWORD || "";

app.use(cors());
app.use(express.json({ limit: "4mb" }));

let collectionPromise;
async function getCollection() {
  if (!collectionPromise) {
    collectionPromise = (async () => {
      const client = new SeekdbClient({ host, port: seekdbPort, user, password, database });
      const schema = new Schema({
        vectorIndex: new VectorIndexConfig({
          hnsw: { dimension: 384, distance: "cosine" },
          embeddingFunction: null,
        }),
        fulltextIndex: new FulltextIndexConfig("ik"),
      });
      return client.getOrCreateCollection({ name: "browser_documents", schema });
    })();
  }
  return collectionPromise;
}

app.get("/api/health", (_req, res) => {
  res.json({ ok: true, seekdb: `${host}:${seekdbPort}/${database}`, embedding: "browser-local" });
});

app.post("/api/documents", async (req, res) => {
  const { id, text, title, embedding } = req.body;
  if (!id || !text || !Array.isArray(embedding) || embedding.length !== 384) {
    return res.status(400).json({ error: "id, text and a 384-dimensional embedding are required" });
  }
  try {
    const collection = await getCollection();
    await collection.upsert({
      ids: [id],
      documents: [text],
      embeddings: [embedding],
      metadatas: [{ title: title || id }],
    });
    await collection.refresh_index();
    return res.json({ ok: true, id });
  } catch (error) {
    return res.status(500).json({ error: String(error) });
  }
});

app.post("/api/search", async (req, res) => {
  const { query, embedding, limit = 5 } = req.body;
  if (!query || !Array.isArray(embedding) || embedding.length !== 384) {
    return res.status(400).json({ error: "query and a 384-dimensional embedding are required" });
  }
  try {
    const collection = await getCollection();
    const result = await collection.hybridSearch({
      query: { whereDocument: { $contains: query } },
      knn: { queryEmbeddings: [embedding] },
      nResults: Math.min(Math.max(Number(limit), 1), 20),
    });
    return res.json({ query, results: result });
  } catch (error) {
    return res.status(500).json({ error: String(error) });
  }
});

const root = path.dirname(fileURLToPath(import.meta.url));
app.use(express.static(path.resolve(root, "../web")));
app.listen(port, () => console.log(`stepfun app listening on http://127.0.0.1:${port}`));
