// El cotizador es un modal que se abre desde LeadDetail.
// Esta página es un redirect a Leads con un mensaje explicativo.
import { useNavigate } from "react-router-dom";
import { Calculator, ArrowLeft } from "lucide-react";
import { useEffect } from "react";
import { useMonzaTheme } from "./MonzaLayout";

export default function MonzaCotizadorPage() {
  const navigate = useNavigate();
  // Sin el tema, el título y los textos salían en gris oscuro sobre el fondo oscuro de
  // MonzaParts: ilegibles. Misma causa que los campos invisibles de Cotizaciones.
  const { dark } = useMonzaTheme();
  const text = dark ? "#E2E8F0" : "#1E293B";
  const muted = dark ? "#94A3B8" : "#64748B";
  const faint = dark ? "#6B7A99" : "#94A3B8";
  useEffect(() => {
    const t = setTimeout(() => navigate("/monzaparts/leads"), 3000);
    return () => clearTimeout(t);
  }, [navigate]);

  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: 80, textAlign: "center" }}>
      <Calculator size={48} className="monza-ic" style={{ marginBottom: 20 }} />
      <h2 style={{ color: text, marginBottom: 8 }}>Calculadora de precios</h2>
      <p style={{ color: muted, maxWidth: 400 }}>
        La calculadora se abre directamente desde el detalle de cada lead, en la sección <strong>Ítems → Calcular todos</strong>.
      </p>
      <p style={{ color: faint, fontSize: 13 }}>Redirigiendo a Leads en 3 segundos...</p>
      <button onClick={() => navigate("/monzaparts/leads")}
        style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 16, padding: "10px 18px", border: "none", borderRadius: 8, background: "var(--monza-accent)", color: "white", cursor: "pointer", fontSize: 13, fontWeight: 600 }}>
        <ArrowLeft size={14} /> Ir a Leads ahora
      </button>
    </div>
  );
}
