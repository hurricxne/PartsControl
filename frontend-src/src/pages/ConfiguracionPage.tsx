import { useState, useEffect } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { Settings, Save, RefreshCw, Shield, Info } from 'lucide-react'
import toast from 'react-hot-toast'
import { cotizadorAPI } from '../services/api'

// Parámetros según hoja "Configuración" del Excel Cotizador MACHPARTS
const PARAMS_META = [
  {
    key: 'tipo_cambio_usd',
    label: 'Tipo de cambio dólar',
    unit: 'CLP/USD',
    factor: 1,
    decimals: 0,
    nota: 'CLP por cada dólar USD. Se actualiza según mercado.',
    format: (v: number) => `$${v.toLocaleString('es-CL')} CLP/USD`,
  },
  {
    key: 'costo_shipping_usd_kg',
    label: 'Costo shipping',
    unit: 'USD/kg',
    factor: 1,
    decimals: 2,
    nota: 'Flete aéreo por kilogramo. Antofagasta: 3,8 / Santiago: 2,7',
    format: (v: number) => `US$${v.toFixed(2)}/kg`,
  },
  {
    key: 'adicionales_shipping_usd',
    label: 'Adicionales shipping (AWB)',
    unit: 'USD',
    factor: 1,
    decimals: 0,
    nota: 'Costos fijos guía aérea (Air Waybill). Se distribuyen proporcionalmente por peso.',
    format: (v: number) => `US$${v.toLocaleString('en-US')}`,
  },
  {
    key: 'costo_agencia_pct',
    label: 'Costo agencia aduana',
    unit: '%',
    factor: 100,
    decimals: 1,
    nota: '% del CIF total. Ejemplo: 1 = 1% del CIF.',
    format: (v: number) => `${(v * 100).toFixed(1)}%`,
  },
  {
    key: 'costo_agencia_minimo_clp',
    label: 'Mínimo agencia aduana',
    unit: 'CLP',
    factor: 1,
    decimals: 0,
    nota: 'Cobro mínimo independiente del % calculado.',
    format: (v: number) => `$${v.toLocaleString('es-CL')}`,
  },
  {
    key: 'desconsolidado_clp',
    label: 'Desconsolidado',
    unit: 'CLP',
    factor: 1,
    decimals: 0,
    nota: 'Costo fijo de desconsolidación del embarque.',
    format: (v: number) => `$${v.toLocaleString('es-CL')}`,
  },
  {
    key: 'bodegaje_clp',
    label: 'Bodegaje',
    unit: 'CLP',
    factor: 1,
    decimals: 0,
    nota: 'Costo fijo de bodegaje del embarque.',
    format: (v: number) => `$${v.toLocaleString('es-CL')}`,
  },
  {
    key: 'margen_venta_pct',
    label: 'Margen de venta por defecto',
    unit: '%',
    factor: 100,
    decimals: 1,
    nota: 'Margen aplicado por defecto a cada ítem. Se puede ajustar por ítem en el Cotizador.',
    format: (v: number) => `${(v * 100).toFixed(1)}%`,
  },
  {
    key: 'plazo_min_default',
    label: 'Plazo de entrega mínimo',
    unit: 'días',
    factor: 1,
    decimals: 0,
    nota: 'Días hábiles mínimo de entrega. Se pre-llena en cada ítem del cotizador como valor por defecto.',
    format: (v: number) => `${Math.round(v)} días`,
  },
  {
    key: 'plazo_max_default',
    label: 'Plazo de entrega máximo',
    unit: 'días',
    factor: 1,
    decimals: 0,
    nota: 'Días hábiles máximo de entrega. Se pre-llena en cada ítem del cotizador como valor por defecto.',
    format: (v: number) => `${Math.round(v)} días`,
  },
] as const

type ParamKey = typeof PARAMS_META[number]['key']
type ConfigData = Record<ParamKey, number>

export default function ConfiguracionPage() {
  const [values, setValues] = useState<Partial<Record<ParamKey, string>>>({})
  const [saved, setSaved] = useState(false)

  // Cargar config desde API
  const { data, isLoading, refetch } = useQuery<ConfigData>({
    queryKey: ['cotizador-config'],
    queryFn: () => cotizadorAPI.getConfig().then(r => r.data),
  })

  useEffect(() => {
    if (!data) return
    const initial: Partial<Record<ParamKey, string>> = {}
    for (const p of PARAMS_META) {
      const raw = data[p.key] as number
      // Convertir a valor de display (factor para %)
      initial[p.key] = p.factor === 100
        ? (raw * 100).toFixed(p.decimals)
        : raw.toFixed(p.decimals)
    }
    setValues(initial)
    setSaved(false)
  }, [data])

  const saveMutation = useMutation({
    mutationFn: (payload: Record<string, number>) => cotizadorAPI.updateConfig(payload),
    onSuccess: () => {
      toast.success('Configuración guardada correctamente')
      setSaved(true)
      refetch()
    },
    onError: () => toast.error('Error al guardar la configuración'),
  })

  const handleSave = () => {
    const payload: Record<string, number> = {}
    for (const p of PARAMS_META) {
      const raw = parseFloat((values[p.key] || '0').replace(',', '.'))
      if (isNaN(raw)) {
        toast.error(`Valor inválido en: ${p.label}`)
        return
      }
      // Convertir de vuelta a valor de API
      payload[p.key] = p.factor === 100 ? raw / 100 : raw
    }
    saveMutation.mutate(payload)
  }

  const handleChange = (key: ParamKey, val: string) => {
    setValues(prev => ({ ...prev, [key]: val }))
    setSaved(false)
  }

  // Valor formateado para visualización
  const getDisplayValue = (key: ParamKey): string => {
    if (!data) return '—'
    const meta = PARAMS_META.find(p => p.key === key)!
    return meta.format(data[key] as number)
  }

  return (
    <div className="space-y-6 max-w-4xl">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>
              Configuración del Cotizador
            </h1>
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium border bg-red-500/10 text-red-400 border-red-400/30">
              <Shield className="w-3 h-3" /> Solo Admin
            </span>
          </div>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
            Parámetros globales de cálculo · Hoja "Configuración" del Cotizador MACHPARTS
          </p>
        </div>
        <button
          onClick={() => refetch()}
          className="btn-secondary text-sm shrink-0"
        >
          <RefreshCw className="w-4 h-4" />
          Actualizar
        </button>
      </div>

      {/* Tabla de parámetros */}
      <div className="card overflow-hidden">
        <div className="flex items-center gap-2 px-5 py-4 border-b" style={{ borderColor: 'var(--border)' }}>
          <Settings className="w-4 h-4 text-brand-400" />
          <h2 className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>
            Parámetros de Cálculo
          </h2>
          <span className="text-xs px-2 py-0.5 rounded-full border" style={{ color: 'var(--text-faint)', borderColor: 'var(--border)' }}>
            Afectan todas las cotizaciones nuevas
          </span>
        </div>

        {isLoading ? (
          <div className="flex items-center justify-center p-10 gap-3" style={{ color: 'var(--text-faint)' }}>
            <RefreshCw className="w-5 h-5 animate-spin" />
            Cargando parámetros...
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr style={{ borderBottom: '1px solid var(--border)', backgroundColor: 'var(--surface-200)' }}>
                  {['Parámetro', 'Valor actual', 'Nuevo valor', 'Notas'].map(h => (
                    <th key={h} className="text-left px-5 py-3 text-xs font-semibold uppercase tracking-wider"
                      style={{ color: 'var(--text-faint)' }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {PARAMS_META.map((p, i) => (
                  <tr
                    key={p.key}
                    style={{ borderBottom: i < PARAMS_META.length - 1 ? '1px solid var(--border)' : undefined }}
                  >
                    <td className="px-5 py-3.5">
                      <div>
                        <p className="font-medium" style={{ color: 'var(--text-primary)' }}>{p.label}</p>
                        <p className="text-xs mt-0.5" style={{ color: 'var(--text-faint)' }}>Campo: {p.key}</p>
                      </div>
                    </td>
                    <td className="px-5 py-3.5">
                      <span className="font-mono font-semibold text-brand-400 text-sm">
                        {getDisplayValue(p.key)}
                      </span>
                    </td>
                    <td className="px-5 py-3.5">
                      <div className="flex items-center gap-1.5">
                        <input
                          type="number"
                          step={p.decimals === 0 ? '1' : `0.${'0'.repeat(p.decimals - 1)}1`}
                          value={values[p.key] ?? ''}
                          onChange={e => handleChange(p.key, e.target.value)}
                          className="w-28 px-2.5 py-1.5 rounded-lg border text-sm outline-none focus:border-brand-500 transition-colors"
                          style={{
                            backgroundColor: 'var(--surface)',
                            borderColor: 'var(--border)',
                            color: 'var(--text-primary)',
                          }}
                        />
                        <span className="text-xs shrink-0" style={{ color: 'var(--text-faint)' }}>
                          {p.unit}
                        </span>
                      </div>
                    </td>
                    <td className="px-5 py-3.5">
                      <div className="flex items-start gap-1.5 max-w-xs">
                        <Info className="w-3 h-3 mt-0.5 shrink-0 text-brand-400" />
                        <span className="text-xs leading-relaxed" style={{ color: 'var(--text-muted)' }}>
                          {p.nota}
                        </span>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Fórmula de cálculo (referencia visual) */}
      <div className="card p-5">
        <div className="flex items-center gap-2 mb-4">
          <Settings className="w-4 h-4 text-brand-400" />
          <h3 className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>
            Referencia — Fórmula de Cálculo (2 pasadas)
          </h3>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 text-xs" style={{ color: 'var(--text-muted)' }}>
          <div className="rounded-xl p-4 space-y-1.5" style={{ backgroundColor: 'var(--surface-200)' }}>
            <p className="font-semibold text-blue-400 mb-2">Pasada 1 — CIF por ítem</p>
            <p>Shipping = Peso(kg) × Costo Shipping × TC</p>
            <p>Adic. AWB = (Peso_ítem / Peso_total) × AWB_fijo × TC</p>
            <p className="font-semibold mt-2" style={{ color: 'var(--text-primary)' }}>
              CIF = (Ex Work × TC) + Shipping + Adic. AWB
            </p>
          </div>
          <div className="rounded-xl p-4 space-y-1.5" style={{ backgroundColor: 'var(--surface-200)' }}>
            <p className="font-semibold text-violet-400 mb-2">Pasada 2 — Precio de venta</p>
            <p>Gastos Loc. totales = MAX(Min. Agencia, % Agencia × CIF total) + Descons. + Bodegaje</p>
            <p>Gastos Loc. ítem = (CIF_ítem / CIF_total) × Gastos totales</p>
            <p>Costo Unit. = (CIF + Gastos Loc.) / Cantidad</p>
            <p className="font-semibold mt-2" style={{ color: 'var(--text-primary)' }}>
              Precio Venta = Costo Unit. × (1 + Margen%)
            </p>
          </div>
        </div>
      </div>

      {/* Acción guardar */}
      <div className="flex items-center gap-3">
        <button
          onClick={handleSave}
          disabled={saveMutation.isPending || isLoading}
          className="btn-primary"
        >
          {saveMutation.isPending
            ? <RefreshCw className="w-4 h-4 animate-spin" />
            : <Save className="w-4 h-4" />
          }
          {saveMutation.isPending ? 'Guardando...' : 'Guardar configuración'}
        </button>
        {saved && (
          <span className="text-sm text-green-400 flex items-center gap-1">
            ✓ Guardado correctamente
          </span>
        )}
      </div>
    </div>
  )
}
