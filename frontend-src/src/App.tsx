import { Routes, Route, Navigate } from 'react-router-dom'
import { useAuthStore } from './stores/authStore'
import LoginPage from './pages/LoginPage'
import DashboardLayout from './pages/DashboardLayout'
import DashboardPage from './pages/DashboardPage'
import ValidaCotizacion from './modules/ValidaCotizacion/ValidaCotizacion'
import CotizadorEditor from './modules/CotizadorEditor/CotizadorEditor'
import CotizacionesFormalesPage from './pages/CotizacionesFormalesPage'
import VentasPage from './pages/VentasPage'
import PreEmbarquesPage from './pages/PreEmbarquesPage'
import EmbarquesPage from './pages/EmbarquesPage'
import ControlTCPage from './pages/ControlTCPage'
import ClientesPage from './pages/ClientesPage'
import ReportesPage from './pages/ReportesPage'
import ComprasPage from './pages/ComprasPage'
import BodegaPage from './pages/BodegaPage'
import DespachosPage from './pages/DespachosPage'
import FacturasPage from './pages/FacturasPage'
import ConfiguracionPage from './pages/ConfiguracionPage'
import CierreVentaPage from './pages/CierreVentaPage'
import CotizacionManual from './modules/CotizacionManual/CotizacionManual'
import SeguimientoPage from './pages/SeguimientoPage'
import VentasContabPage from './pages/VentasContabPage'
// Módulo aislado Embarques Pricing (costo landed). Para revertir: volver a
// './pages/EmbarquesPricingPage' (mockup) y quitar la carpeta src/embarques-pricing/.
import EmbarquesPricingPage from './embarques-pricing/EmbarquesPricingPage'
// Módulo aislado Compras / Cuentas por Pagar. Para revertir: quitar este import,
// la ruta 'compras-contab' y la carpeta src/compras-contab/.
import ComprasContabPage from './compras-contab/ComprasContabPage'
// Módulo aislado Tesorería (Grupo AM): aprobación de pagos + conciliación bancaria
// (cargos↔egresos y abonos↔cobranzas) + flujo de caja NIC 7. Reemplaza a la antigua
// Conciliación Bancaria. Revertir: quitar este import, las rutas 'tesoreria'/
// 'conciliacion' y la carpeta src/tesoreria/.
import TesoreriaPage from './tesoreria/TesoreriaPage'
import UsuariosPage from './pages/UsuariosPage'
import ProveedoresPage from './pages/ProveedoresPage'

function PrivateRoute({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated())
  const user = useAuthStore((s) => s.user)

  if (typeof document !== 'undefined') {
    document.documentElement.setAttribute('data-empresa', user?.empresa || 'automotriz')
  }

  return isAuthenticated ? <>{children}</> : <Navigate to="/login" replace />
}


// MonzaParts
import MonzaDashboardPage from './pages/MonzaDashboardPage'
import MonzaLayout from './pages/MonzaLayout'
import MonzaLeadsPage from './pages/MonzaLeadsPage'
import MonzaCotizadorPage from './pages/MonzaCotizadorPage'
import MonzaCotizacionesPage from './pages/MonzaCotizacionesPage'
import MonzaVentasPage from './pages/MonzaVentasPage'
import MonzaConfigPage from './pages/MonzaConfigPage'
import MonzaDespachosPage from './pages/MonzaDespachosPage'
import MonzaLogsPage from './pages/MonzaLogsPage'
import MonzaAbastecimientoPage from './pages/MonzaAbastecimientoPage'
import MonzaSeguimientoPage from './pages/MonzaSeguimientoPage'
import MonzaLogisticaPage from './pages/MonzaLogisticaPage'
import MonzaBodegaPage from './pages/MonzaBodegaPage'
import MonzaPerfilPage from './pages/MonzaPerfilPage'
// Módulo aislado Contabilidad MonzaParts (Ventas + Facturas/Cobranzas). Revertir: quitar
// estos imports, las rutas 'ventas-contab'/'facturas' y el grupo Contabilidad en MonzaLayout.
import MonzaVentasContabPage from './pages/MonzaVentasContabPage'
import MonzaFacturasPage from './pages/MonzaFacturasPage'
import MonzaEmbarquesPricingPage from './pages/MonzaEmbarquesPricingPage'
import MonzaComprasPage from './pages/MonzaComprasPage'
import MonzaTesoreriaPage from './pages/MonzaTesoreriaPage'

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/"
        element={
          <PrivateRoute>
            <DashboardLayout />
          </PrivateRoute>
        }
      >
        <Route index element={<DashboardPage />} />
        <Route path="cotizaciones"  element={<ValidaCotizacion />} />
        <Route path="cotizaciones/:id/editor" element={<CotizadorEditor />} />
        <Route path="cotizaciones/nueva-manual" element={<CotizacionManual />} />
        <Route path="cot-formales"  element={<CotizacionesFormalesPage />} />
        <Route path="ventas"        element={<VentasPage />} />
        <Route path="embarques"     element={<PreEmbarquesPage />} />
        <Route path="embarques-list" element={<EmbarquesPage />} />
        <Route path="control-tc"    element={<ControlTCPage />} />
        <Route path="clientes"      element={<ClientesPage />} />
        <Route path="reportes"      element={<ReportesPage />} />
        <Route path="compras"       element={<ComprasPage />} />
        <Route path="bodega"        element={<BodegaPage />} />
        <Route path="despachos"     element={<DespachosPage />} />
        <Route path="facturas"      element={<FacturasPage />} />
        <Route path="configuracion" element={<ConfiguracionPage />} />
        <Route path="cierre-venta"  element={<CierreVentaPage />} />
        <Route path="seguimiento"   element={<SeguimientoPage />} />
        <Route path="ventas-contab" element={<VentasContabPage />} />
        <Route path="embarques-pricing" element={<EmbarquesPricingPage />} />
        <Route path="compras-contab" element={<ComprasContabPage />} />
        <Route path="tesoreria" element={<TesoreriaPage />} />
        {/* ruta antigua del módulo (hoy Tesorería): redirige por memoria muscular */}
        <Route path="conciliacion" element={<Navigate to="/tesoreria" replace />} />
        <Route path="usuarios"      element={<UsuariosPage />} />
        <Route path="proveedores"   element={<ProveedoresPage />} />
      </Route>

      {/* MonzaParts */}
      <Route
        path="/monzaparts"
        element={
          <PrivateRoute>
            <MonzaLayout />
          </PrivateRoute>
        }
      >
        <Route index element={<MonzaDashboardPage />} />
        <Route path="dashboard" element={<MonzaDashboardPage />} />
        <Route path="leads" element={<MonzaLeadsPage />} />
        <Route path="cotizador" element={<MonzaCotizadorPage />} />
        <Route path="cotizaciones" element={<MonzaCotizacionesPage />} />
        <Route path="ventas" element={<MonzaVentasPage />} />
        <Route path="ventas-contab" element={<MonzaVentasContabPage />} />
        <Route path="facturas" element={<MonzaFacturasPage />} />
        <Route path="compras-contab" element={<MonzaComprasPage />} />
        <Route path="tesoreria" element={<MonzaTesoreriaPage />} />
        <Route path="embarques-pricing" element={<MonzaEmbarquesPricingPage />} />
        <Route path="abastecimiento" element={<MonzaAbastecimientoPage />} />
        <Route path="seguimiento" element={<MonzaSeguimientoPage />} />
        <Route path="logistica" element={<MonzaLogisticaPage />} />
        <Route path="bodega" element={<MonzaBodegaPage />} />
        <Route path="perfil" element={<MonzaPerfilPage />} />
        <Route path="despachos"      element={<MonzaDespachosPage />} />
        <Route path="logs"           element={<MonzaLogsPage />} />
        <Route path="configuracion" element={<MonzaConfigPage />} />
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
