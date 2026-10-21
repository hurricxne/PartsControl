"""Integración PartsFlow → MonzaParts (2026-10-21): POST /api/monza/integraciones/partsflow/leads.

Cada bloque es un caso de prueba del documento entregado al proveedor
(features/monza/integracion-partsflow-leads.feature).
Datos MARCADOS `__TEST_PF__` (external_id, nombre de cliente); limpieza total en `finally`
verificada con sesión nueva. La clave se fija en la prueba: nunca la del .env.
Corre con: cd backend && ./venv/bin/python -m pytest monza_tests/test_integracion_partsflow.py -q -s
"""
import os
import sys
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import monza_router_integraciones as integ  # noqa: E402
from config import settings  # noqa: E402
from database import SessionLocal  # noqa: E402
from monza_models import (MonzaCliente, MonzaLead, MonzaLeadItem, MonzaNotificacion,  # noqa: E402
                          MonzaWebhookLog)

MARK = "__TEST_PF__"
CLAVE = "clave-de-prueba-partsflow-" + "x" * 16
URL = "/api/monza/integraciones/partsflow/leads"

app = FastAPI()
app.include_router(integ.router)
client = TestClient(app)
_fails = []


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _post(cuerpo, clave=CLAVE, **kw):
    headers = {"X-API-Key": clave} if clave else {}
    return client.post(URL, json=cuerpo, headers=headers, **kw)


def _minimo(eid, telefono="+56900000001", nombre=None):
    return {"external_id": f"{MARK}{eid}", "contacto": {"nombre": nombre or f"{MARK} Cliente {eid}",
                                                         "telefono": telefono}}


def _lead(numero):
    db = SessionLocal()
    try:
        lead = db.query(MonzaLead).filter(MonzaLead.numero == numero).first()
        if not lead:
            return None, [], None
        items = db.query(MonzaLeadItem).filter(MonzaLeadItem.lead_id == lead.id).all()
        cli = db.query(MonzaCliente).filter(MonzaCliente.id == lead.cliente_id).first()
        db.expunge_all()
        return lead, items, cli
    finally:
        db.close()


def run():
    # ══ 1 · Lead completo ══
    completo = {
        "external_id": f"{MARK}completo", "creado_en": "2026-10-21T15:42:00-03:00",
        "contacto": {"nombre": f"{MARK} Juan Pérez", "telefono": "+56900000002",
                     "email": "juan.__test_pf__@taller.invalid", "empresa": "Taller Pérez"},
        "vehiculo": {"patente": "abcd12", "vin": "WDD2050421F123456", "marca": "Mercedes-Benz",
                     "modelo": "C 200", "anio": 2016, "motor": "M274 2.0"},
        "repuestos": [{"descripcion": "Variador leva escape izquierdo", "cantidad": 1},
                      {"descripcion": "Bomba de agua", "cantidad": 2, "numero_parte": "A2742000107"}],
        "comentario": "Cliente pide original y alternativo.", "urgencia": "alta",
        "url_conversacion": "https://app.partsflow.example/conversaciones/8f3a2c91",
        "campo_desconocido": "se ignora",
    }
    r = _post(completo)
    check("1a lead completo → 201 con número", r.status_code == 201 and r.json().get("lead_numero", "").startswith("L-"),
          (r.status_code, r.text[:200]))
    numero_completo = r.json().get("lead_numero") if r.status_code == 201 else None
    lead, items, cli = _lead(numero_completo)
    check("1b canal PartsFlow, pendiente, línea autos, VIN y año",
          lead is not None and lead.canal_origen == "PartsFlow" and lead.estado == "pendiente"
          and lead.linea == "autos" and lead.vin == "WDD2050421F123456" and lead.anio == "2016",
          lead and (lead.canal_origen, lead.estado, lead.vin, lead.anio))
    check("1c dos repuestos con su cantidad y número de parte",
          sorted((i.descripcion, i.cantidad, i.numero_parte) for i in items)
          == [("Bomba de agua", 2, "A2742000107"), ("Variador leva escape izquierdo", 1, None)],
          [(i.descripcion, i.cantidad, i.numero_parte) for i in items])
    check("1d patente (en mayúsculas), urgencia, empresa y enlace quedan en el comentario",
          lead is not None and all(x in (lead.comentario or "") for x in
                                   ("Patente: ABCD12", "Urgencia: ALTA", "Empresa: Taller Pérez",
                                    "Conversación PartsFlow: https://", "original y alternativo")),
          lead and lead.comentario)
    check("1e la respuesta dice que el cliente era nuevo", r.status_code == 201 and r.json().get("cliente_existente") is False,
          r.text[:200])

    # ══ 2 · Lead mínimo ══
    r = _post(_minimo("minimo", telefono="+56900000003"))
    check("2 lead mínimo → 201", r.status_code == 201, (r.status_code, r.text[:200]))

    # ══ 3 · Duplicado: misma solicitud dos veces ══
    r2 = _post(completo)
    check("3 la misma solicitud otra vez → 200, mismo número, duplicado",
          r2.status_code == 200 and r2.json().get("duplicado") is True
          and r2.json().get("lead_numero") == numero_completo, (r2.status_code, r2.text[:200]))

    # ══ 4 · Dos envíos simultáneos → un solo lead ══
    resultados = []
    cuerpo = _minimo("simultaneo", telefono="+56900000004")
    hilos = [threading.Thread(target=lambda: resultados.append(_post(cuerpo))) for _ in range(2)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    numeros = {x.json().get("lead_numero") for x in resultados}
    check("4 dos envíos simultáneos → el mismo número (un solo lead)",
          len(numeros) == 1 and sorted(x.status_code for x in resultados) == [200, 201],
          [(x.status_code, x.text[:80]) for x in resultados])

    # ══ 5 · Cliente existente (teléfono guardado en otro formato) ══
    db = SessionLocal()
    try:
        ficha = MonzaCliente(nombre=f"{MARK} Existente", telefono="9 0000 0005")
        db.add(ficha)
        db.commit()
        ficha_id = ficha.id
    finally:
        db.close()
    r = _post(_minimo("existente", telefono="+56900000005", nombre=f"{MARK} Existente"))
    lead, _, cli = _lead(r.json().get("lead_numero")) if r.status_code == 201 else (None, [], None)
    check("5 cliente existente → misma ficha y cliente_existente true",
          r.status_code == 201 and r.json().get("cliente_existente") is True and cli is not None and cli.id == ficha_id,
          (r.status_code, r.text[:200], cli and cli.id, ficha_id))

    # ══ 6 · Datos mal formados → 422 con el campo, sin lead ══
    malo = _minimo("telefono-malo", telefono="912345678")
    r = _post(malo)
    campos = [e.get("campo") for e in r.json().get("errores", [])] if r.status_code == 422 else []
    check("6a teléfono sin código de país → 422 en contacto.telefono",
          r.status_code == 422 and "contacto.telefono" in campos, (r.status_code, r.text[:200]))
    largo = _minimo("largo", telefono="+56900000006")
    largo["repuestos"] = [{"descripcion": "x" * 301}]
    r = _post(largo)
    campos = [e.get("campo") for e in r.json().get("errores", [])] if r.status_code == 422 else []
    check("6b descripción de 301 caracteres → 422 en repuestos[0].descripcion",
          r.status_code == 422 and "repuestos[0].descripcion" in campos, (r.status_code, r.text[:200]))
    r = _post({"contacto": {"telefono": "+56900000007"}})
    campos = sorted(e.get("campo") for e in r.json().get("errores", [])) if r.status_code == 422 else []
    check("6c faltan external_id y nombre → 422 con los dos campos",
          campos == ["contacto.nombre", "external_id"], (r.status_code, campos))
    rut_malo = _minimo("rut", telefono="+56900000008")
    rut_malo["contacto"]["rut"] = "76.123.456-7"  # DV correcto: 0
    r = _post(rut_malo)
    check("6d RUT con dígito verificador malo → 422 en contacto.rut",
          r.status_code == 422 and any(e.get("campo") == "contacto.rut" for e in r.json().get("errores", [])),
          (r.status_code, r.text[:200]))
    db = SessionLocal()
    try:
        n = (db.query(MonzaWebhookLog).filter(MonzaWebhookLog.fuente == "partsflow",
                                              MonzaWebhookLog.external_id.in_(
                                                  [f"{MARK}telefono-malo", f"{MARK}largo", f"{MARK}rut"]),
                                              MonzaWebhookLog.procesado == 1).count())
        check("6e ningún rechazo creó un lead (y quedaron registrados para revisión)", n == 0, n)
    finally:
        db.close()

    # ══ 7 · Clave ══
    r = _post(_minimo("clave-mala"), clave="otra-clave")
    check("7a clave incorrecta → 401", r.status_code == 401, r.status_code)
    r = _post(_minimo("sin-clave"), clave=None)
    check("7b sin clave → 401", r.status_code == 401, r.status_code)
    r = client.post(URL + f"?key={CLAVE}", json=_minimo("clave-url"))
    check("7c clave en la URL (no en la cabecera) → 401", r.status_code == 401, r.status_code)
    r = client.post(URL, content=b"{no es json", headers={"X-API-Key": CLAVE, "Content-Type": "application/json"})
    check("7d cuerpo que no es JSON → 400", r.status_code == 400, r.status_code)
    r = client.post(URL, content=b"[" + b"1," * 40000 + b"1]",
                    headers={"X-API-Key": CLAVE, "Content-Type": "application/json"})
    check("7e cuerpo sobre 64 KB → 413", r.status_code == 413, r.status_code)

    # ══ 8 · Límite por minuto ══
    original = integ.PF_MAX_POR_MINUTO
    integ.PF_MAX_POR_MINUTO = 3
    integ._PF_MARCAS.clear()
    try:
        # Cuerpos marcados e incompletos (sin contacto): pasan el cupo y vuelven 422.
        codigos = [_post({"external_id": f"{MARK}cupo{i}"}).status_code for i in range(4)]
        ultimo = _post({"external_id": f"{MARK}cupo9"})
        check("8 sobre el límite → 429 con Retry-After",
              codigos[:3] == [422, 422, 422] and codigos[3] == 429 and ultimo.headers.get("Retry-After"),
              (codigos, dict(ultimo.headers)))
    finally:
        integ.PF_MAX_POR_MINUTO = original
        integ._PF_MARCAS.clear()


def cleanup():
    db = SessionLocal()
    try:
        logs = db.query(MonzaWebhookLog).filter(MonzaWebhookLog.fuente == "partsflow",
                                                MonzaWebhookLog.external_id.like(f"{MARK}%")).all()
        lead_ids = {l.lead_id for l in logs if l.lead_id}
        clientes = db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).all()
        cli_ids = {c.id for c in clientes}
        lead_ids |= {l.id for l in db.query(MonzaLead).filter(MonzaLead.cliente_id.in_(cli_ids or {0})).all()}
        if lead_ids:
            db.query(MonzaNotificacion).filter(MonzaNotificacion.entidad == "lead",
                                               MonzaNotificacion.entidad_id.in_(lead_ids)).delete(synchronize_session=False)
            db.query(MonzaLeadItem).filter(MonzaLeadItem.lead_id.in_(lead_ids)).delete(synchronize_session=False)
            db.query(MonzaLead).filter(MonzaLead.id.in_(lead_ids)).delete(synchronize_session=False)
        if cli_ids:
            db.query(MonzaCliente).filter(MonzaCliente.id.in_(cli_ids)).delete(synchronize_session=False)
        db.query(MonzaWebhookLog).filter(MonzaWebhookLog.fuente == "partsflow",
                                         MonzaWebhookLog.external_id.like(f"{MARK}%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    db = SessionLocal()
    try:
        quedan = (db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).count()
                  + db.query(MonzaWebhookLog).filter(MonzaWebhookLog.external_id.like(f"{MARK}%")).count())
        print("Cleanup BD OK" if quedan == 0 else f"Cleanup INCOMPLETO: quedan {quedan}")
    finally:
        db.close()


def test_integracion_partsflow():
    original = settings.PARTSFLOW_API_KEY
    settings.PARTSFLOW_API_KEY = CLAVE
    integ._PF_MARCAS.clear()
    try:
        run()
    finally:
        settings.PARTSFLOW_API_KEY = original
        cleanup()
    print()
    print("RESULTADO:", "TODO OK" if not _fails else f"{len(_fails)} fallas: {_fails}")
    assert not _fails, f"fallas: {_fails}"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-s"]))
