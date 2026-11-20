"""
Monitor OBD2 via Bluetooth LE (BLE) para adaptadores ELM327 baratos.
Mostra velocidade, RPM e temperatura do liquido de arrefecimento.

Uso:  py obd_monitor.py            (procura o adaptador sozinho)
      py obd_monitor.py ENDERECO   (usa um endereco Bluetooth especifico)
Ctrl+C para sair.
"""
import asyncio
import datetime
import os
import re
import sys

try:
    import msvcrt  # teclado no Windows
except ImportError:
    msvcrt = None

from bleak import BleakClient, BleakScanner

ARQUIVO_EVENTOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "luz_oleo.csv")


def tecla_s_apertada():
    """True se a tecla S foi apertada desde a ultima checagem."""
    if msvcrt is None:
        return False
    apertou = False
    while msvcrt.kbhit():
        if msvcrt.getwch().lower() == "s":
            apertou = True
    return apertou


def registrar_evento(vel, rpm, temp):
    """Grava a marcacao no arquivo luz_oleo.csv e devolve a hora."""
    agora = datetime.datetime.now()
    novo = not os.path.exists(ARQUIVO_EVENTOS)
    with open(ARQUIVO_EVENTOS, "a", encoding="utf-8-sig") as f:
        if novo:
            f.write("data_hora;temp_arrefecimento_C;rpm;velocidade_kmh\n")
        f.write(
            f"{agora:%Y-%m-%d %H:%M:%S};"
            f"{'' if temp is None else temp};"
            f"{'' if rpm is None else rpm};"
            f"{'' if vel is None else vel}\n"
        )
    return f"{agora:%H:%M:%S}"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

NOMES = ("obd", "elm", "link", "vgate", "icar", "veepeak", "scan")
SERVICOS_PADRAO = ("00001800", "00001801", "0000180a")


async def escolher_dispositivo(endereco):
    if endereco:
        return endereco
    print("Procurando adaptador Bluetooth LE (8 segundos)...")
    achados = await BleakScanner.discover(timeout=8.0)
    candidatos = [d for d in achados if d.name and any(n in d.name.lower() for n in NOMES)]
    if not candidatos:
        print("Nenhum adaptador encontrado. Dispositivos vistos:")
        for d in achados:
            print(f"  {d.name or '(sem nome)'}  {d.address}")
        print("\nConfira: adaptador no carro, ignicao ligada, app do celular fechado.")
        print("Se ele aparecer acima com outro nome, rode: py obd_monitor.py ENDERECO")
        return None
    if len(candidatos) == 1:
        d = candidatos[0]
        print(f"Encontrado: {d.name} ({d.address})")
        return d.address
    print("Mais de um adaptador encontrado:")
    for i, d in enumerate(candidatos, 1):
        print(f"  {i}) {d.name}  {d.address}")
    while True:
        r = input("Digite o numero do seu adaptador: ").strip()
        if r.isdigit() and 1 <= int(r) <= len(candidatos):
            return candidatos[int(r) - 1].address


def achar_caracteristicas(client):
    """Acha a caracteristica de notificacao (RX) e a de escrita (TX)."""
    for svc in client.services:
        if svc.uuid.lower().startswith(SERVICOS_PADRAO):
            continue
        rx = tx = None
        for ch in svc.characteristics:
            p = ch.properties
            if rx is None and ("notify" in p or "indicate" in p):
                rx = ch
            if tx is None and ("write" in p or "write-without-response" in p):
                tx = ch
        if rx and tx:
            return rx, tx
    # plano B: caracteristicas em servicos diferentes
    rx = tx = None
    for svc in client.services:
        if svc.uuid.lower().startswith(SERVICOS_PADRAO):
            continue
        for ch in svc.characteristics:
            p = ch.properties
            if rx is None and ("notify" in p or "indicate" in p):
                rx = ch
            if tx is None and ("write" in p or "write-without-response" in p):
                tx = ch
    return rx, tx


class ELM:
    def __init__(self, client, rx, tx):
        self.client = client
        self.rx = rx
        self.tx = tx
        self.buf = ""
        self.evt = asyncio.Event()
        self.com_resposta = "write" in tx.properties

    def _ao_receber(self, _, dados):
        self.buf += dados.decode("ascii", "ignore")
        if ">" in self.buf:
            self.evt.set()

    async def iniciar(self):
        await self.client.start_notify(self.rx, self._ao_receber)

    async def cmd(self, comando, timeout=6.0):
        self.buf = ""
        self.evt.clear()
        await self.client.write_gatt_char(
            self.tx, (comando + "\r").encode(), response=self.com_resposta
        )
        try:
            await asyncio.wait_for(self.evt.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        return re.sub(r"[\r\n>]", " ", self.buf).strip()

    async def pid(self, pid):
        """Consulta um PID do modo 01 e devolve os bytes de dados (ou None)."""
        resp = await self.cmd("01" + pid)
        s = resp.replace(" ", "").upper()
        i = s.find("41" + pid)
        if i < 0:
            return None
        hexa = s[i + 4:]
        try:
            return [int(hexa[j:j + 2], 16) for j in range(0, len(hexa) - 1, 2)]
        except ValueError:
            return None


async def main():
    endereco = await escolher_dispositivo(sys.argv[1] if len(sys.argv) > 1 else None)
    if not endereco:
        return

    print("Conectando...")
    async with BleakClient(endereco, timeout=15.0) as client:
        rx, tx = achar_caracteristicas(client)
        if not rx or not tx:
            print("Nao achei as caracteristicas de comunicacao deste adaptador.")
            print("Servicos encontrados:")
            for svc in client.services:
                print(" ", svc.uuid)
                for ch in svc.characteristics:
                    print("    ", ch.uuid, ch.properties)
            return

        elm = ELM(client, rx, tx)
        await elm.iniciar()

        print("Iniciando adaptador...")
        await elm.cmd("ATZ", 4.0)
        for c in ("ATE0", "ATL0", "ATS0", "ATH0", "ATSP0"):
            await elm.cmd(c)

        print("Falando com o carro (pode levar alguns segundos)...")
        teste = await elm.cmd("0100", 12.0)
        if "UNABLE" in teste.upper() or "ERROR" in teste.upper():
            print("O adaptador nao conseguiu falar com o carro. Ligue a ignicao e tente de novo.")
            return

        print("Conectado! Aperte S quando a luz do óleo acender. Ctrl+C para sair.")
        print(f"Os registros ficam em: {ARQUIVO_EVENTOS}\n")
        eventos = 0
        while True:
            vel = await elm.pid("0D")
            rpm = await elm.pid("0C")
            tmp = await elm.pid("05")
            vel_n = vel[0] if vel else None
            rpm_n = (rpm[0] * 256 + rpm[1]) // 4 if rpm and len(rpm) >= 2 else None
            tmp_n = tmp[0] - 40 if tmp else None
            v = f"{vel_n:>3}" if vel_n is not None else "---"
            r = f"{rpm_n:>5}" if rpm_n is not None else "-----"
            t = f"{tmp_n:>3}" if tmp_n is not None else "---"

            if tecla_s_apertada():
                hora = registrar_evento(vel_n, rpm_n, tmp_n)
                print(f"\n>>> LUZ DO ÓLEO ACENDEU às {hora} | arrefecimento: {t.strip()} °C | "
                      f"RPM: {r.strip()} | {v.strip()} km/h")
                eventos += 1

            print(f"\rVel: {v} km/h  RPM: {r}  Arrefecimento: {t} °C  "
                  f"[S = luz do óleo acendeu | marcados: {eventos}]   ", end="", flush=True)
            await asyncio.sleep(0.2)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nEncerrado.")
    except Exception as e:
        print(f"\nErro: {e}")
    input("\nPressione Enter para fechar...")
