import { useState, useEffect, useRef, useCallback } from "react";
import { Paperclip, Upload, Download, Trash2, FileText } from "lucide-react";
import { monzaDocumentosAPI } from "../services/monzaApi";
import { useMonzaTheme } from "./MonzaLayout";
import toast from "react-hot-toast";

interface Doc {
  id: number; categoria: string; original_name?: string; filename: string;
  content_type?: string; fecha?: string;
}

interface Props {
  entidad: string;
  entidadId: number;
  /** categorías sugeridas para el selector al subir */
  categorias?: string[];
  titulo?: string;
}

export default function MonzaDocs({ entidad, entidadId, categorias = ["factura", "guía", "tracking", "foto", "otro"], titulo = "Documentos" }: Props) {
  const { dark } = useMonzaTheme();
  const [docs, setDocs] = useState<Doc[]>([]);
  const [loading, setLoading] = useState(false);
  const [cat, setCat] = useState(categorias[0]);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const bd = dark ? "#1e2a4a" : "#E2E8F0";
  const txt = dark ? "#e2e8f0" : "#1E293B";
  const sub = dark ? "#8899cc" : "#64748B";
  const panel = dark ? "#0d1321" : "#F8FAFC";

  const load = useCallback(async () => {
    setLoading(true);
    try { const r = await monzaDocumentosAPI.list(entidad, entidadId); setDocs(r.data); }
    catch {} finally { setLoading(false); }
  }, [entidad, entidadId]);

  useEffect(() => { load(); }, [load]);

  const onUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) e.target.value = "";
    if (!file) return;
    if (file.size > 10 * 1024 * 1024) { toast.error("Máx 10 MB"); return; }
    setUploading(true);
    try {
      await monzaDocumentosAPI.upload(entidad, entidadId, file, cat);
      toast.success("Documento subido");
      load();
    } catch { toast.error("Error al subir"); }
    finally { setUploading(false); }
  };

  const onDownload = async (d: Doc) => {
    try {
      const r = await monzaDocumentosAPI.download(d.id);
      const blob = new Blob([r.data], { type: d.content_type || "application/octet-stream" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = d.original_name || d.filename;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch { toast.error("Error al descargar"); }
  };

  const onDelete = async (d: Doc) => {
    if (!window.confirm("¿Eliminar este documento?")) return;
    try { await monzaDocumentosAPI.remove(d.id); load(); toast.success("Eliminado"); }
    catch { toast.error("Error"); }
  };

  return (
    <div style={{ border: `1px solid ${bd}`, borderRadius: 8, padding: "10px 12px", background: panel }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
        <span style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, fontWeight: 700, color: sub, textTransform: "uppercase", letterSpacing: 0.4 }}>
          <Paperclip size={13} /> {titulo} {docs.length > 0 && <span style={{ color: "var(--monza-accent)" }}>({docs.length})</span>}
        </span>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <select value={cat} onChange={(e) => setCat(e.target.value)}
            style={{ padding: "4px 6px", border: `1px solid ${bd}`, borderRadius: 5, fontSize: 11, background: dark ? "#131b3e" : "white", color: txt }}>
            {categorias.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
          <input ref={fileRef} type="file" style={{ display: "none" }} onChange={onUpload} />
          <button onClick={() => fileRef.current?.click()} disabled={uploading}
            style={{ display: "flex", alignItems: "center", gap: 4, padding: "4px 10px", background: "var(--monza-accent)", border: "none", borderRadius: 5, color: "white", cursor: "pointer", fontSize: 11, fontWeight: 600 }}>
            <Upload size={11} /> {uploading ? "Subiendo..." : "Subir"}
          </button>
        </div>
      </div>

      {loading ? (
        <div style={{ fontSize: 11, color: sub, padding: "6px 0" }}>Cargando...</div>
      ) : docs.length === 0 ? (
        <div style={{ fontSize: 11, color: sub, padding: "6px 0" }}>Sin documentos adjuntos.</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {docs.map((d) => (
            <div key={d.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "5px 8px", background: dark ? "#131b3e" : "white", border: `1px solid ${bd}`, borderRadius: 6 }}>
              <FileText size={13} color={sub} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12, color: txt, fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{d.original_name || d.filename}</div>
                <div style={{ fontSize: 10, color: sub }}>{d.categoria}{d.fecha ? ` · ${new Date(d.fecha).toLocaleDateString("es-CL")}` : ""}</div>
              </div>
              <button onClick={() => onDownload(d)} title="Descargar" style={{ background: "transparent", border: `1px solid ${bd}`, borderRadius: 5, cursor: "pointer", color: "#0EA5E9", padding: "3px 5px", display: "flex" }}>
                <Download size={12} />
              </button>
              <button onClick={() => onDelete(d)} title="Eliminar" style={{ background: "transparent", border: `1px solid ${bd}`, borderRadius: 5, cursor: "pointer", color: "#EF4444", padding: "3px 5px", display: "flex" }}>
                <Trash2 size={12} />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
