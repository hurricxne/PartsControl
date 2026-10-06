import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { useAuthStore, esCuentaPortal } from './stores/authStore'
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
import TicketsPage from './pages/TicketsPage'
// Libro de Compras del SII (Grupo AM, Fases A1+A2): tablero + bandeja + reglas por RUT.
// Revertir: quitar este import, la ruta 'libro-sii' y la entrada del menú en DashboardLayout.
import LibroSiiPage from './pages/LibroSiiPage'
// Portal de Clientes. Dos mundos distintos en la misma carpeta (src/portal/):
//   · '/portal/*' → lo que ve el CLIENTE, fuera de los dos cascarones internos.
//     PortalLayout es su cascarón propio (cabecera, datos de la sesión, cambio de la
//     clave temporal) y adentro van sus pantallas: inicio, pedidos, una orden, el
//     seguimiento de un ítem y la cuenta.
//   · CuentasPortalPage → '/cuentas-portal', pantalla INTERNA de Grupo AM para crear y
//     administrar esas cuentas. La ruta interna NO empieza con '/portal': DashboardLayout
//     resuelve el título por prefijo y la confundiría con la del cliente.
// Revertir: quitar estos imports, PortalRoute, RutaDesconocida, el desvío portal de
// PrivateRoute, el bloque de rutas '/portal', la ruta 'cuentas-portal', la entrada del
// menú Admin en DashboardLayout y la carpeta src/portal/.
import PortalLayout from './portal/PortalLayout'
import TableroPage from './portal/TableroPage'
import PedidosPage from './portal/PedidosPage'
import OrdenPage from './portal/OrdenPage'
import ItemPage from './portal/ItemPage'
import CuentaPage from './portal/CuentaPage'
import CuentasPortalPage from './portal/CuentasPortalPage'

// Guard de los DOS cascarones internos (MachParts y MonzaParts).
function PrivateRoute({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated())
  const user = useAuthStore((s) => s.user)
  const location = useLocation()

  if (typeof document !== 'undefined') {
    // Para cuentas del equipo Bigcode (@bigcode.cl) el tema sigue a la SECCIÓN visitada
    // (pueden trabajar en ambos sistemas); para el resto, según la empresa de su cuenta.
    const esBigcode = (user?.email || '').toLowerCase().endsWith('@bigcode.cl')
    const empresaTema = esBigcode
      ? (location.pathname.startsWith('/monzaparts') ? 'automotriz' : 'mineria')
      : (user?.empresa || 'automotriz')
    document.documentElement.setAttribute('data-empresa', empresaTema)
  }

  if (!isAuthenticated) return <Navigate to="/login" replace />
  // Una cuenta del Portal de Clientes no entra a NINGUNA ruta interna. El desvío va acá y
  // no en cada layout porque este es el único componente por el que pasan los dos
  // cascarones: puesto en DashboardLayout, /monzaparts/* quedaría abierto. Se corta ANTES
  // de montar el layout, que al montarse ya pide contadores y notificaciones del ERP.
  // Es solo ruteo (el backend le responde 403 a ese token en todo el ERP): evita que el
  // cliente vea el menú interno con las tablas vacías.
  if (esCuentaPortal(user)) return <Navigate to="/portal" replace />
  return <>{children}</>
}

// Guard de '/portal': el espejo de PrivateRoute. El personal no navega el portal con su
// cuenta (el backend le respondería 403: la vista "todos los clientes" no existe a
// propósito), así que vuelve a su inicio de siempre.
function PortalRoute({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated())
  const user = useAuthStore((s) => s.user)

  if (!isAuthenticated) return <Navigate to="/login" replace />
  if (!esCuentaPortal(user)) {
    return <Navigate to={user?.empresa === 'automotriz' ? '/monzaparts/leads' : '/'} replace />
  }
  return <>{children}</>
}

// Comodín. El personal sigue cayendo en '/' como siempre. La cuenta portal va directo a
// '/portal': mandarla a '/' también terminaría ahí (PrivateRoute la desvía), pero con un
// salto de más por una ruta interna que no tiene por qué pisar.
function RutaDesconocida() {
  const user = useAuthStore((s) => s.user)
  return <Navigate to={esCuentaPortal(user) ? '/portal' : '/'} replace />
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
import MonzaTicketsPage from './pages/MonzaTicketsPage'
import MonzaLogsPage from './pages/MonzaLogsPage'
import MonzaAbastecimientoPage from './pages/MonzaAbastecimientoPage'
import MonzaSeguimientoPage from './pages/MonzaSeguimientoPage'
import MonzaLogisticaPage from './pages/MonzaLogisticaPage'
import MonzaBodegaPage from './pages/MonzaBodegaPage'
import MonzaPerfilPage from './pages/MonzaPerfilPage'
// Módulo aislado Contabilidad MonzaParts (Ventas + Facturas/Cobranzas). Revertir: quitar
// estos imports, las rutas 'ventas-contab'/'facturas' y el grupo Contabilidad en MonzaLayout.
import MonzaVentasContabPage from './pages/MonzaVentasContabPage'

// Contabilidad MonzaParts: apagable en build-time (VITE_MONZA_CONTAB=false en PROD).
const MONZA_CONTAB = import.meta.env.VITE_MONZA_CONTAB !== 'false' 
import MonzaFacturasPage from './pages/MonzaFacturasPage'
import MonzaEmbarquesPricingPage from './pages/MonzaEmbarquesPricingPage'
import MonzaComprasPage from './pages/MonzaComprasPage'
// Libro de Compras del SII MonzaParts (Fases A1+A2, espejo de LibroSiiPage GA): tablero +
// bandeja + conciliación bancaria + reglas por RUT. Revertir: quitar este import, la ruta
// 'libro-sii' del layout Monza y la entrada del menú Contabilidad en MonzaLayout.
import MonzaLibroSiiPage from './pages/MonzaLibroSiiPage'
import MonzaTesoreriaPage from './pages/MonzaTesoreriaPage'

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      {/* Portal de Clientes: fuera de DashboardLayout y MonzaLayout a propósito. Las
          llaves de las URL son ids: el N° de OC es texto libre y no sirve de llave. */}
      <Route
        path="/portal"
        element={
          <PortalRoute>
            <PortalLayout />
          </PortalRoute>
        }
      >
        <Route index element={<TableroPage />} />
        <Route path="pedidos" element={<PedidosPage />} />
        <Route path="pedidos/:ordenId" element={<OrdenPage />} />
        <Route path="pedidos/:ordenId/items/:itemId" element={<ItemPage />} />
        <Route path="cuenta" element={<CuentaPage />} />
        {/* Cualquier otra dirección bajo /portal vuelve al inicio del portal. */}
        <Route path="*" element={<Navigate to="/portal" replace />} />
      </Route>
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
        <Route path="libro-sii" element={<LibroSiiPage />} />
        <Route path="tesoreria" element={<TesoreriaPage />} />
        {/* ruta antigua del módulo (hoy Tesorería): redirige por memoria muscular */}
        <Route path="conciliacion" element={<Navigate to="/tesoreria" replace />} />
        <Route path="usuarios"      element={<UsuariosPage />} />
        <Route path="proveedores"   element={<ProveedoresPage />} />
        <Route path="tickets"       element={<TicketsPage />} />
        <Route path="cuentas-portal" element={<CuentasPortalPage />} />
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
        {MONZA_CONTAB && <Route path="ventas-contab" element={<MonzaVentasContabPage />} />}
        <Route path="facturas" element={<MonzaFacturasPage />} />
        {MONZA_CONTAB && <Route path="compras-contab" element={<MonzaComprasPage />} />}
        {MONZA_CONTAB && <Route path="libro-sii" element={<MonzaLibroSiiPage />} />}
        {MONZA_CONTAB && <Route path="tesoreria" element={<MonzaTesoreriaPage />} />}
        {MONZA_CONTAB && <Route path="embarques-pricing" element={<MonzaEmbarquesPricingPage />} />}
        <Route path="abastecimiento" element={<MonzaAbastecimientoPage />} />
        <Route path="seguimiento" element={<MonzaSeguimientoPage />} />
        <Route path="logistica" element={<MonzaLogisticaPage />} />
        <Route path="bodega" element={<MonzaBodegaPage />} />
        <Route path="perfil" element={<MonzaPerfilPage />} />
        <Route path="despachos"      element={<MonzaDespachosPage />} />
        <Route path="tickets"        element={<MonzaTicketsPage />} />
        <Route path="logs"           element={<MonzaLogsPage />} />
        <Route path="configuracion" element={<MonzaConfigPage />} />
      </Route>

      <Route path="*" element={<RutaDesconocida />} />
    </Routes>
  )
}
