// «Mi cuenta» (ruta /portal/cuenta): con qué cuenta entró la persona, a qué empresas
// representa y el cambio de su clave.
//
// No pide nada: los datos son los de GET /api/portal/yo, que PortalLayout ya cargó y
// reparte por sesion.ts::usePortal. El cambio de clave es CambiarClaveForm (POST
// /api/portal/yo/clave).
//
// Al perfil 'bodega' se le DICE que su cuenta no muestra montos: sin esa línea, las
// rayas «—» donde otros ven precios parecen una falla.
import { Building2 } from 'lucide-react'
import { usePortal, useTituloDePestana } from './sesion'
import { Aviso } from './piezas'
import CambiarClaveForm from './CambiarClaveForm'

export default function CuentaPage() {
  const { yo, veMontos } = usePortal()
  useTituloDePestana('Mi cuenta')

  return (
    <div className="portal-entrada">
      <p className="portal-rotulo">Tu acceso a MachParts</p>
      <h1 className="mt-2 text-3xl sm:text-4xl font-extrabold tracking-tight">Mi cuenta</h1>
      <p className="portal-suave mt-2 text-base">
        {yo.usuario.nombre ? `${yo.usuario.nombre} · ` : ''}{yo.usuario.email}
      </p>

      {/* Dos columnas desde lg: a la izquierda lo que se LEE (quién es), a la derecha lo
          único que se puede HACER acá (la clave). En un teléfono se apilan en ese orden. */}
      <div className="mt-8 grid gap-6 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)] lg:items-start">
        <div className="space-y-6 min-w-0">
          <section className="portal-tarjeta p-5 sm:p-7" aria-labelledby="portal-empresas">
            <h2 id="portal-empresas" className="text-lg font-bold tracking-tight">
              {yo.clientes.length === 1 ? 'Empresa que representas' : 'Empresas que representas'}
            </h2>
            <ul className="mt-3">
              {yo.clientes.map((c) => (
                <li key={c.id} className="portal-fila flex items-center gap-3.5 py-3.5">
                  <span className="portal-inicial w-11 h-11 rounded-xl flex items-center justify-center shrink-0" aria-hidden="true">
                    <Building2 className="w-5 h-5" />
                  </span>
                  <div className="min-w-0">
                    <p className="font-semibold text-base">{c.nombre || 'Empresa sin nombre registrado'}</p>
                    <p className="portal-suave text-sm">
                      {c.rut ? <>RUT <span className="portal-codigo">{c.rut}</span></> : 'RUT no registrado'}
                    </p>
                  </div>
                </li>
              ))}
            </ul>
          </section>

          {!veMontos && (
            <Aviso tono="info">
              Tu cuenta es de <strong>bodega</strong>: muestra dónde va cada pedido y qué se despachó,
              sin montos ni facturas. Si necesitas verlos, pídele el cambio a tu ejecutivo de MachParts.
            </Aviso>
          )}
        </div>

        <section className="portal-tarjeta p-5 sm:p-7 min-w-0" aria-labelledby="portal-clave">
          <h2 id="portal-clave" className="text-lg font-bold tracking-tight">Cambiar mi clave</h2>
          <p className="portal-suave mt-1.5 mb-5 text-sm">
            Usa una clave que solo tú conozcas. Si la olvidas, tu ejecutivo de MachParts puede generarte una nueva.
          </p>
          <CambiarClaveForm />
        </section>
      </div>
    </div>
  )
}
