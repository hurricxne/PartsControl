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
import EmbarquesPricingPage from './pages/EmbarquesPricingPage'
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
        <Route path="usuarios"      element={<UsuariosPage />} />
        <Route path="proveedores"   element={<ProveedoresPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
