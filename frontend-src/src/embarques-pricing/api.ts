// Cliente API del módulo Embarques Pricing. Reutiliza la instancia axios
// compartida (auth + manejo de 401 ya configurados en services/api.ts).
import api from '../services/api'
import type { EmbarquePricingRow, PricingDetail, PricingSavePayload } from './types'

export const embarquesPricingAPI = {
  list: (q?: string) =>
    api.get<EmbarquePricingRow[]>('/embarques-pricing', {
      params: q ? { q } : {},
    }),
  get: (embarqueId: number) =>
    api.get<PricingDetail>(`/embarques-pricing/${embarqueId}`),
  save: (embarqueId: number, data: PricingSavePayload) =>
    api.put<PricingDetail>(`/embarques-pricing/${embarqueId}`, data),
  cerrar: (embarqueId: number) =>
    api.post<PricingDetail>(`/embarques-pricing/${embarqueId}/cerrar`),
  reabrir: (embarqueId: number) =>
    api.post<PricingDetail>(`/embarques-pricing/${embarqueId}/reabrir`),
}
