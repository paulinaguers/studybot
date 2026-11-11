import csv
import html
import io
import json
import os
import re
import threading
import time
from datetime import date, datetime
from xml.etree import ElementTree
import tempfile

from dotenv import load_dotenv
from flask import Flask, request  # <-- Agregamos Flask y request
from google import genai
import requests
import telebot
from telebot import types

load_dotenv(override=False)

# Usamos las variables de entorno estándar para Vercel
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")

if not TELEGRAM_TOKEN:
  raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en el archivo .env")

if not GEMINI_API_KEY:
  raise RuntimeError("Falta GEMINI_API_KEY en el archivo .env")

# Configuramos threaded=False para entornos serverless
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML", threaded=False)
cliente_gemini = genai.Client(api_key=GEMINI_API_KEY)
MODELO = "models/gemini-3.5-flash"

app = Flask(__name__)  # <-- Inicializamos Flask para Vercel

print("StudyBot preparado para servidorless.")

# --- (Toda tu lógica de diccionarios, tareas, comandos y handlers sigue exactamente igual acá abajo) ---

# Cada chat tiene sus propios datos académicos.
materias_por_usuario = {}
examenes_por_usuario = {}
repasos_por_usuario = {}
documentos_por_usuario = {}  # chat_id -> materia -> lista de documentos


# Directorio seguro: usa /tmp si está en Vercel, o la carpeta actual si estás en tu PC
BASE_DIR = "/tmp" if os.getenv("VERCEL") else "."
ARCHIVO_HISTORIAL = os.path.join(BASE_DIR, "historial.csv")
ARCHIVO_TAREAS = os.path.join(BASE_DIR, "tareas_studybot.json")


def obtener_materias(chat_id):
  if chat_id not in materias_por_usuario:
    materias_por_usuario[chat_id] = {}
  return materias_por_usuario[chat_id]


def obtener_examenes(chat_id):
  if chat_id not in examenes_por_usuario:
    examenes_por_usuario[chat_id] = {}
  return examenes_por_usuario[chat_id]


pendientes_documentos = {}



def cargar_tareas():
  try:
    with open(ARCHIVO_TAREAS, "r", encoding="utf-8") as archivo:
      datos = json.load(archivo)
    return datos if isinstance(datos, dict) else {}
  except (FileNotFoundError, json.JSONDecodeError, OSError):
    return {}


tareas_por_usuario = cargar_tareas()


def tareas_del_chat(chat_id):
  return tareas_por_usuario.setdefault(str(chat_id), [])


def guardar_tareas():
  with open(ARCHIVO_TAREAS, "w", encoding="utf-8") as archivo:
    json.dump(tareas_por_usuario, archivo, ensure_ascii=False, indent=2)


def mostrar_checklist(chat_id):
  tareas = tareas_del_chat(chat_id)
  botones = types.InlineKeyboardMarkup(row_width=1)
  if not tareas:
    texto = (
        "<b>✅ Mi checklist</b>\n\nTodavía no tenés tareas. Agregá una con"
        " /agregartarea."
    )
  else:
    lineas = ["<b>✅ Mi checklist</b>", ""]
    for i, tarea in enumerate(tareas, 1):
      marca = "✅" if tarea["hecha"] else "⬜"
      lineas.append(f"{i}. {marca} {html.escape(tarea['texto'])}")
      botones.add(
          types.InlineKeyboardButton(
              f"{marca} {i}. {tarea['texto'][:38]}",
              callback_data=f"tarea_toggle_{i-1}",
          )
      )
    texto = "\n".join(lineas)
  botones.add(
      types.InlineKeyboardButton("➕ Agregar tarea", callback_data="tarea_agregar")
  )
  if tareas:
    botones.add(
        types.InlineKeyboardButton(
            "🗑️ Borrar tareas completadas", callback_data="tarea_limpiar"
        )
    )
  bot.send_message(chat_id, texto, reply_markup=botones)


def solicitar_tarea(chat_id):
  mensaje = bot.send_message(
      chat_id, "✏️ Escribí la tarea que querés agregar (máximo 150 caracteres)."
  )
  bot.register_next_step_handler(mensaje, recibir_nueva_tarea)


def recibir_nueva_tarea(message):
  if cancelar_si_es_comando(message):
    return
  registrar_interaccion(message)
  texto = (message.text or "").strip()
  if not texto or len(texto) > 150:
    bot.send_message(
        message.chat.id,
        "⚠️ Escribí una tarea de entre 1 y 150 caracteres. Usá /agregartarea"
        " para volver a intentar.",
    )
    return
  tareas_del_chat(message.chat.id).append({"texto": texto, "hecha": False})
  guardar_tareas()
  bot.send_message(message.chat.id, "✅ Tarea agregada.")
  mostrar_checklist(message.chat.id)


def registrar_interaccion(message):
  existe = os.path.exists(ARCHIVO_HISTORIAL)
  with open(ARCHIVO_HISTORIAL, "a", newline="", encoding="utf-8") as archivo:
    escritor = csv.writer(archivo)
    if not existe:
      escritor.writerow(["timestamp", "chat_id", "nombre", "mensaje"])
    nombre = message.from_user.first_name or "Sin nombre"
    mensaje = message.text or f"[{message.content_type}]"
    escritor.writerow([
        datetime.now().isoformat(timespec="seconds"),
        message.chat.id,
        nombre,
        mensaje,
    ])


def consultar_gemini(texto, modelo=MODELO):
  for intento in range(4):
    try:
      respuesta = cliente_gemini.models.generate_content(
          model=modelo, contents=texto
      )
      return respuesta.text
    except Exception as error:
      print("Error Gemini:", error)
      if intento < 3:
        time.sleep(5)
  return None


def menu_inline():
  teclado = types.InlineKeyboardMarkup()
  teclado.row(
      types.InlineKeyboardButton("📚 Repasar", callback_data="repasar"),
      types.InlineKeyboardButton("📅 Exámenes", callback_data="examenes"),
  )
  teclado.row(
      types.InlineKeyboardButton("🧠 Método de estudio", callback_data="metodo"),
      types.InlineKeyboardButton("🕐 Horarios", callback_data="horarios"),
  )
  return teclado


def cancelar_si_es_comando(message):
  texto = (message.text or "").strip()
  if texto.startswith("/"):
    bot.clear_step_handler_by_chat_id(message.chat.id)
    bot.process_new_messages([message])
    return True
  return False


def interpretar_fecha(texto):
  try:
    return datetime.strptime(texto.strip(), "%d/%m/%Y").date()
  except ValueError:
    return None


def validar_horario(texto):
  patron = r"^(Lunes|Martes|Miércoles|Miercoles|Jueves|Viernes|Sábado|Sabado|Domingo)(?:\s+y\s+(?:Lunes|Martes|Miércoles|Miercoles|Jueves|Viernes|Sábado|Sabado|Domingo))*\s+([01]\d|2[0-3]):[0-5]\d$"
  return re.fullmatch(patron, texto.strip(), flags=re.IGNORECASE) is not None


def pregunta_multiple_choice(chat_id):
  repaso = repasos_por_usuario.get(chat_id)
  if not repaso:
    return
  indice = repaso["indice"]
  preguntas = repaso["preguntas"]
  if indice >= len(preguntas):
    bot.send_message(
        chat_id,
        "🏁 Terminaste el repaso. Resultado:"
        f" <b>{repaso['aciertos']}/{len(preguntas)}</b> respuestas correctas.",
    )
    repasos_por_usuario.pop(chat_id, None)
    return
  pregunta = preguntas[indice]
  teclado = types.InlineKeyboardMarkup()
  for letra in "ABCD":
    teclado.add(
        types.InlineKeyboardButton(letra, callback_data=f"quiz_{letra}")
    )
  opciones = "\n".join(
      f"{letra}) {html.escape(str(pregunta['opciones'][letra]))}"
      for letra in "ABCD"
  )
  bot.send_message(
      chat_id,
      f"📚 <b>Pregunta {indice+1}/{len(preguntas)}</b>\n\n"
      f"{html.escape(str(pregunta['pregunta']))}\n\n{opciones}",
      reply_markup=teclado,
  )


@bot.message_handler(commands=["start"])
def comando_start(message):
  registrar_interaccion(message)
  nombre = message.from_user.first_name or "persona"
  texto = (
      f"¡Hola, <b>{nombre}</b>! 👋\n\nSoy <b>StudyBot</b>, tu asistente de"
      " estudio.\nTe ayudo a organizar materias, recordar exámenes y afianzar"
      " contenidos. 📚\n\n<i>Elegí una opción para empezar:</i>"
  )
  bot.send_message(message.chat.id, texto, reply_markup=menu_inline())


@bot.message_handler(commands=["ayuda"])
def comando_ayuda(message):
  registrar_interaccion(message)
  texto = (
      "<b>📚 Guía de comandos de StudyBot</b>\n\n<b>🤖 Interactuar con el"
      " bot:</b>\n/start — iniciar el bot\n/reiniciar — borrar materias,"
      " horarios y exámenes\n/historial — ver tus últimas 5"
      " consultas\n/atajos — mostrar botones rápidos\n/fin —"
      " finalizar\n\n<b>📝 Configurá tu cursada:</b>\n/agregartarea — agregar"
      " una tarea\n/agregarmateria — cargar una materia\n/agregarexamen —"
      " cargar un examen\n\n<b>📅 Organizate:</b>\n/materias — ver"
      " materias\n/horarios — ver horarios de cursada\n/examenes — ver"
      " próximos exámenes\n/calendario — ver materias, horarios y"
      " exámenes\n/recordatorio — días hasta el próximo examen\n/tareas — ver"
      " checklist y marcar tareas\n\n<b>📚 Momento de estudiar:</b>\n/repasar —"
      " hacer un multiple choice en el chat\n/ayuda — ver todos los comandos"
      " disponibles\n/metodo — pedir un método de estudio\n/plan — calcular"
      " cuánto estudiar por día\n/guia — recibir una guía de"
      " estudio\n\n<b>🔗 Recursos extra:</b>\n/notebooklm — abrir NotebookLM"
      " para estudiar con tus fuentes\n/ambiente — abrir un espacio de estudio"
      " en LifeAt\n\n"
  )
  bot.send_message(message.chat.id, texto)


@bot.message_handler(commands=["materias"])
def comando_materias(message):
  registrar_interaccion(message)
  materias = obtener_materias(message.chat.id)
  if not materias:
    bot.send_message(
        message.chat.id,
        "📚 Todavía no tenés materias cargadas. Usá /agregarmateria para"
        " agregar una.",
    )
    return
  texto = "<b>📚 Tus materias</b>\n\n"
  for materia, datos in materias.items():
    texto += f"• <b>{materia}</b>: {datos['contenidos']}\n"
  bot.send_message(message.chat.id, texto)


@bot.message_handler(commands=["horarios"])
def comando_horarios(message):
  registrar_interaccion(message)
  materias = obtener_materias(message.chat.id)
  if not materias:
    bot.send_message(
        message.chat.id,
        "🕐 Todavía no tenés horarios cargados. Usá /agregarmateria para"
        " agregar una materia y su horario.",
    )
    return
  texto = "<b>🕐 Horarios de cursada</b>\n\n"
  for materia, datos in materias.items():
    texto += f"• {materia}: {datos['horario']}\n"
  bot.send_message(message.chat.id, texto)


@bot.message_handler(commands=["examenes"])
def comando_examenes(message):
  registrar_interaccion(message)
  examenes = obtener_examenes(message.chat.id)
  if not examenes:
    bot.send_message(
        message.chat.id,
        "📅 Todavía no tenés exámenes cargados. Usá /agregarexamen para agregar"
        " uno.",
    )
    return
  texto = "<b>📅 Próximos exámenes</b>\n\n"
  for materia, fecha in examenes.items():
    texto += f"• <b>{materia}</b>: {fecha}\n"
  bot.send_message(message.chat.id, texto)


@bot.message_handler(commands=["atajos"])
def comando_atajos(message):
  registrar_interaccion(message)
  bot.send_message(
      message.chat.id, "Elegí una opción 👇", reply_markup=menu_inline()
  )


@bot.message_handler(commands=["fin"])
def comando_fin(message):
  registrar_interaccion(message)
  bot.send_message(
      message.chat.id,
      "¡Hasta la próxima! 👋📚\nNo te olvides de mantener tus materias al"
      " día.",
  )


@bot.message_handler(commands=["repasar"])
def comando_repasar(message):
  registrar_interaccion(message)
  materias = obtener_materias(message.chat.id)
  if not materias:
    bot.send_message(
        message.chat.id,
        "📚 Primero cargá una materia con /agregarmateria. Así puedo hacer el"
        " repaso usando solamente los contenidos que estás estudiando.",
    )
    return
  nombres = ", ".join(materias.keys())
  mensaje = bot.send_message(
      message.chat.id,
      f"📚 ¿Qué materia querés repasar? Tenés cargadas: <b>{nombres}</b>",
  )
  bot.register_next_step_handler(mensaje, generar_repaso)


def generar_repaso(message):
  if cancelar_si_es_comando(message):
    return
  registrar_interaccion(message)
  materias = obtener_materias(message.chat.id)
  elegido = (message.text or "").strip()
  nombre_materia = None
  for nombre in materias:
    if nombre.lower() == elegido.lower():
      nombre_materia = nombre
      break
  if nombre_materia is None:
    nombres = ", ".join(materias.keys())
    bot.send_message(
        message.chat.id,
        "No encontré esa materia entre las que cargaste. Tus materias son:"
        f" <b>{nombres}</b>. Usá /repasar para intentar de nuevo.",
    )
    return
  contenidos = materias[nombre_materia]["contenidos"]
  bot.send_message(
      message.chat.id, "⏳ Estoy preparando tu repaso con los contenidos que cargaste..."
  )
  bot.send_chat_action(message.chat.id, "typing")
  documentos = documentos_por_usuario.get(message.chat.id, {}).get(
      nombre_materia, []
  )
  if documentos:
    fuentes = "\n\n".join(
        f"Documento {d['nombre']}:\n{d['texto']}" for d in documentos
    )
    instruccion_fuente = (
        "Usá EXCLUSIVAMENTE la información de los siguientes documentos para"
        " formular las preguntas. Si el material no alcanza para 5 preguntas"
        ' diferentes, no inventes datos y devolvé {"preguntas": []}.\n'
        + fuentes[:45000]
    )
  else:
    instruccion_fuente = (
        "No hay documentos adjuntos: basate SOLO en los contenidos autorizados."
    )
  prompt = (
      instruccion_fuente
      + "\n"
      + "Sos un profesor que crea un cuestionario multiple choice"
      f" universitario. Materia: {nombre_materia}. Contenidos autorizados:"
      f" {contenidos}. Generá exactamente 5 preguntas SOLO sobre los"
      " contenidos autorizados. No incluyas otros temas. Devolvé SOLO un"
      " objeto JSON válido, sin markdown ni comentarios, con la clave"
      " 'preguntas', una lista de 5 objetos. Cada objeto tiene 'pregunta'"
      " (texto), 'opciones' (objeto con claves A,B,C,D y textos), 'correcta'"
      " (una letra A,B,C,D), y 'explicacion' (una oración breve). Alterná"
      " las respuestas correctas."
  )
  respuesta = consultar_gemini(prompt)
  if not respuesta:
    bot.send_message(
        message.chat.id,
        "No pude generar el repaso en este momento. Probá de nuevo.",
    )
    return
  try:
    limpio = (
        respuesta.strip().replace("```json", "").replace("```", "").strip()
    )
    datos = json.loads(limpio)
    preguntas = datos["preguntas"]
    if len(preguntas) != 5:
      bot.send_message(
          message.chat.id,
          "📄 No hay suficiente información en tus documentos para crear cinco"
          " preguntas. Subí más material o ampliá los contenidos.",
      )
      return
    for pregunta in preguntas:
      if pregunta["correcta"] not in "ABCD" or any(
          letra not in pregunta["opciones"] for letra in "ABCD"
      ):
        raise ValueError("Opciones incorrectas")
    repasos_por_usuario[message.chat.id] = {
        "preguntas": preguntas,
        "indice": 0,
        "aciertos": 0,
    }
    bot.send_message(
        message.chat.id,
        f"🧠 <b>Repaso de {html.escape(nombre_materia)}</b>\nRespondé tocando A,"
        " B, C o D. Te corrijo cada pregunta.",
    )
    pregunta_multiple_choice(message.chat.id)
  except (ValueError, KeyError, TypeError) as error:
    print("Error interpretando el repaso:", error)
    bot.send_message(
        message.chat.id, "No pude preparar el cuestionario. Probá /repasar nuevamente."
    )


@bot.message_handler(commands=["metodo"])
def comando_metodo(message):
  registrar_interaccion(message)
  mensaje = bot.send_message(
      message.chat.id, "🧠 ¿Qué materia o tema necesitás estudiar?"
  )
  bot.register_next_step_handler(mensaje, generar_metodo)


def generar_metodo(message):
  if cancelar_si_es_comando(message):
    return
  registrar_interaccion(message)
  tema = (message.text or "").strip()
  bot.send_message(
      message.chat.id,
      "⏳ Estoy buscando un método de estudio adecuado para ese tema...",
  )
  bot.send_chat_action(message.chat.id, "typing")
  prompt = (
      "Sos un asistente de estudio. Proponé un método concreto para estudiar el"
      f" siguiente tema: {tema}. Indicá pasos simples, tiempo aproximado y"
      " una forma de repasar al final. Sé breve y práctico."
  )
  respuesta = consultar_gemini(prompt)
  if respuesta:
    bot.send_message(message.chat.id, respuesta)
  else:
    bot.send_message(message.chat.id, "No pude consultar Gemini en este momento.")


@bot.message_handler(commands=["plan"])
def comando_plan(message):
  registrar_interaccion(message)
  mensaje = bot.send_message(
      message.chat.id, "📖 ¿Cuántas páginas tenés que estudiar?"
  )
  bot.register_next_step_handler(mensaje, recibir_paginas)


def recibir_paginas(message):
  if cancelar_si_es_comando(message):
    return
  registrar_interaccion(message)
  try:
    paginas = int(message.text)
    mensaje = bot.send_message(
        message.chat.id, "📅 ¿En cuántos días querés terminar?"
    )
    bot.register_next_step_handler(mensaje, calcular_plan, paginas)
  except:
    bot.send_message(
        message.chat.id, "Ingresá la cantidad de páginas usando un número entero."
    )


def calcular_plan(message, paginas):
  if cancelar_si_es_comando(message):
    return
  registrar_interaccion(message)
  try:
    dias = int(message.text)
    if dias <= 0:
      bot.send_message(
          message.chat.id, "La cantidad de días debe ser mayor que cero."
      )
      return
    paginas_por_dia = paginas / dias
    bot.send_message(
        message.chat.id,
        f"✅ Para completar <b>{paginas} páginas</b> en <b>{dias} días</b>,"
        " deberías estudiar aproximadamente <b>"
        f"{paginas_por_dia:.1f} páginas por día</b>.",
    )
  except:
    bot.send_message(
        message.chat.id, "Ingresá la cantidad de días usando un número entero."
    )


@bot.message_handler(commands=["historial"])
def comando_historial(message):
  if not os.path.exists(ARCHIVO_HISTORIAL):
    bot.send_message(message.chat.id, "Todavía no hay consultas registradas.")
    registrar_interaccion(message)
    return
  with open(ARCHIVO_HISTORIAL, "r", encoding="utf-8") as archivo:
    lector = csv.DictReader(archivo)
    filas = [
        fila for fila in lector if str(fila["chat_id"]) == str(message.chat.id)
    ]
  ultimas = filas[-5:]
  if not ultimas:
    bot.send_message(message.chat.id, "Todavía no hay consultas registradas.")
    registrar_interaccion(message)
    return
  texto = "<b>🕘 Tus últimas 5 consultas</b>\n\n"
  for fila in ultimas:
    texto += f"• {fila['mensaje']}\n"
  bot.send_message(message.chat.id, texto)
  registrar_interaccion(message)


@bot.message_handler(commands=["dolar"])
def comando_dolar(message):
  registrar_interaccion(message)
  bot.send_message(message.chat.id, "⏳ Estoy consultando la cotización...")
  bot.send_chat_action(message.chat.id, "typing")
  url = "https://dolarapi.com/v1/dolares/oficial"
  response = requests.get(url)
  if response.status_code == 200:
    data = response.json()
    compra = data["compra"]
    venta = data["venta"]
    texto = f"<b>💵 Dólar oficial</b>\n\nCompra: ${compra}\nVenta: ${venta}"
    bot.send_message(message.chat.id, texto)
  else:
    bot.send_message(
        message.chat.id,
        f"No pude consultar la cotización. Código: {response.status_code}",
    )


@bot.callback_query_handler(func=lambda call: True)
def botones_inline(call):
  chat_id = call.message.chat.id
  materias = obtener_materias(chat_id)
  examenes = obtener_examenes(chat_id)

  if call.data == "tarea_agregar":
    bot.answer_callback_query(call.id)
    solicitar_tarea(chat_id)
    return
  if call.data == "tarea_limpiar":
    tareas_por_usuario[str(chat_id)] = [
        t for t in tareas_del_chat(chat_id) if not t["hecha"]
    ]
    guardar_tareas()
    bot.answer_callback_query(call.id, "Completadas eliminadas")
    bot.edit_message_reply_markup(
        chat_id, call.message.message_id, reply_markup=None
    )
    mostrar_checklist(chat_id)
    return
  if call.data.startswith("tarea_toggle_"):
    try:
      indice = int(call.data.rsplit("_", 1)[1])
      tareas = tareas_del_chat(chat_id)
      if indice < 0 or indice >= len(tareas):
        raise IndexError
      tareas[indice]["hecha"] = not tareas[indice]["hecha"]
      guardar_tareas()
      bot.answer_callback_query(call.id, "Checklist actualizado")
      bot.edit_message_reply_markup(
          chat_id, call.message.message_id, reply_markup=None
      )
      mostrar_checklist(chat_id)
    except (ValueError, IndexError):
      bot.answer_callback_query(call.id, "Esta lista ya cambió. Usá /tareas.")
    return

  if call.data.startswith("quiz_"):
    repaso = repasos_por_usuario.get(chat_id)
    if not repaso:
      bot.answer_callback_query(call.id, "Este repaso ya terminó.")
      return
    pregunta = repaso["preguntas"][repaso["indice"]]
    elegida = call.data[-1]
    correcta = pregunta["correcta"]
    if elegida == correcta:
      repaso["aciertos"] += 1
      resultado = "✅ ¡Correcto!"
    else:
      resultado = f"❌ La correcta era {correcta}."
    bot.answer_callback_query(call.id)
    bot.edit_message_reply_markup(
        chat_id, call.message.message_id, reply_markup=None
    )
    bot.send_message(
        chat_id,
        resultado + "\n" + html.escape(str(pregunta.get("explicacion", ""))),
    )
    repaso["indice"] += 1
    pregunta_multiple_choice(chat_id)
    return

  if call.data == "repasar":
    if not materias:
      bot.send_message(
          chat_id,
          "📚 Primero cargá una materia con /agregarmateria. Así puedo hacer el"
          " repaso usando solamente tus contenidos.",
      )
    else:
      nombres = ", ".join(materias.keys())
      mensaje = bot.send_message(
          chat_id,
          f"📚 ¿Qué materia querés repasar? Tenés cargadas: <b>{nombres}</b>",
      )
      bot.register_next_step_handler(mensaje, generar_repaso)

  elif call.data == "examenes":
    if not examenes:
      bot.send_message(
          chat_id,
          "📅 Todavía no tenés exámenes cargados. Usá /agregarexamen para agregar"
          " uno.",
      )
    else:
      texto = "<b>📅 Próximos exámenes</b>\n\n"
      for materia, fecha in examenes.items():
        texto += f"• <b>{materia}</b>: {fecha}\n"
      bot.send_message(chat_id, texto)

  elif call.data == "metodo":
    mensaje = bot.send_message(
        chat_id, "🧠 ¿Qué materia o tema necesitás estudiar?"
    )
    bot.register_next_step_handler(mensaje, generar_metodo)

  elif call.data == "horarios":
    if not materias:
      bot.send_message(
          chat_id,
          "🕐 Todavía no tenés horarios cargados. Usá /agregarmateria para"
          " agregar una materia y su horario.",
      )
    else:
      texto = "<b>🕐 Horarios de cursada</b>\n\n"
      for materia, datos in materias.items():
        texto += f"• {materia}: {datos['horario']}\n"
      bot.send_message(chat_id, texto)

  elif call.data == "reiniciar_si":
    materias.clear()
    examenes.clear()
    repasos_por_usuario.pop(chat_id, None)
    documentos_por_usuario.pop(chat_id, None)
    pendientes_documentos.pop(chat_id, None)
    bot.send_message(
        chat_id,
        "✅ Listo. Se borraron tus materias, horarios y exámenes. Los datos de"
        " los demás usuarios no se modificaron.",
    )

  elif call.data == "reiniciar_no":
    bot.send_message(chat_id, "👌 No se borró nada.")

  bot.answer_callback_query(call.id)


@bot.message_handler(commands=["agregarmateria"])
def comando_agregar_materia(message):
  registrar_interaccion(message)
  mensaje = bot.send_message(
      message.chat.id, "📚 Escribí el nombre de la materia."
  )
  bot.register_next_step_handler(mensaje, recibir_nombre_materia)


def recibir_nombre_materia(message):
  if cancelar_si_es_comando(message):
    return
  registrar_interaccion(message)
  nombre = (message.text or "").strip()
  mensaje = bot.send_message(
      message.chat.id, "🕐 Escribí el horario de cursada. Ejemplo: Lunes 10:00"
  )
  bot.register_next_step_handler(mensaje, recibir_horario_materia, nombre)


def recibir_horario_materia(message, nombre):
  if cancelar_si_es_comando(message):
    return
  registrar_interaccion(message)
  horario = (message.text or "").strip()
  if not validar_horario(horario):
    mensaje = bot.send_message(
        message.chat.id,
        "⚠️ Horario inválido. Escribí un día y una hora real, por ejemplo: Lunes"
        " 10:00 o Martes y Jueves 14:30",
    )
    bot.register_next_step_handler(mensaje, recibir_horario_materia, nombre)
    return
  mensaje = bot.send_message(
      message.chat.id,
      "📝 Escribí los contenidos principales de la materia. Estos contenidos se"
      " van a usar para generar tus repasos.",
  )
  bot.register_next_step_handler(mensaje, guardar_materia, nombre, horario)


def guardar_materia(message, nombre, horario):
  if cancelar_si_es_comando(message):
    return
  registrar_interaccion(message)
  contenidos = (message.text or "").strip()
  materias = obtener_materias(message.chat.id)
  materias[nombre] = {"horario": horario, "contenidos": contenidos}
  bot.send_message(
      message.chat.id,
      f"✅ Guardé <b>{nombre}</b> correctamente. Ahora /repasar va a usar estos"
      " contenidos.",
  )


@bot.message_handler(commands=["agregarexamen"])
def comando_agregar_examen(message):
  registrar_interaccion(message)
  mensaje = bot.send_message(
      message.chat.id, "📚 Escribí la materia del examen."
  )
  bot.register_next_step_handler(mensaje, recibir_materia_examen)


def recibir_materia_examen(message):
  if cancelar_si_es_comando(message):
    return
  registrar_interaccion(message)
  materia = (message.text or "").strip()
  mensaje = bot.send_message(
      message.chat.id,
      "📅 ¿Qué fecha tiene el examen? Escribila DD/MM/AAAA. Ejemplo:"
      " 20/10/2026",
  )
  bot.register_next_step_handler(mensaje, guardar_examen, materia)


def guardar_examen(message, materia):
  if cancelar_si_es_comando(message):
    return
  registrar_interaccion(message)
  fecha = (message.text or "").strip()
  dia = interpretar_fecha(fecha)
  if dia is None:
    mensaje = bot.send_message(
        message.chat.id,
        "⚠️ Fecha inválida. Escribí una fecha real DD/MM/AAAA, por ejemplo"
        " 20/10/2026.",
    )
    bot.register_next_step_handler(mensaje, guardar_examen, materia)
    return
  if dia < date.today():
    mensaje = bot.send_message(
        message.chat.id,
        "⚠️ Esa fecha ya pasó. Indicá una fecha de examen futura.",
    )
    bot.register_next_step_handler(mensaje, guardar_examen, materia)
    return
  examenes = obtener_examenes(message.chat.id)
  examenes[materia] = fecha
  bot.send_message(
      message.chat.id,
      f"✅ Guardé el examen de <b>{html.escape(materia)}</b> para el {fecha}.",
  )


@bot.message_handler(commands=["calendario"])
def comando_calendario(message):
  registrar_interaccion(message)
  materias = obtener_materias(message.chat.id)
  examenes = obtener_examenes(message.chat.id)
  if not materias and not examenes:
    bot.send_message(
        message.chat.id,
        "🗓️ Tu calendario está vacío. Usá /agregarmateria y /agregarexamen para"
        " empezar.",
    )
    return
  texto = "<b>🗓️ Calendario de estudio</b>\n\n"
  for materia, datos in materias.items():
    texto += f"<b>📚 {materia}</b>\n"
    texto += f"🕐 Cursada: {datos['horario']}\n"
    if materia in examenes:
      texto += f"📝 Examen: {examenes[materia]}\n"
    else:
      texto += "📝 Examen: sin fecha cargada\n"
    texto += "\n"
  for materia, examen in examenes.items():
    if materia not in materias:
      texto += f"<b>📝 {materia}</b>\nExamen: {examen}\n\n"
  bot.send_message(message.chat.id, texto)


@bot.message_handler(commands=["recordatorio"])
def comando_recordatorio(message):
  registrar_interaccion(message)
  examenes = obtener_examenes(message.chat.id)
  if not examenes:
    bot.send_message(
        message.chat.id,
        "📅 Todavía no cargaste ningún examen. Usá /agregarexamen para agregar"
        " una fecha y poder consultar cuánto falta.",
    )
    return
  proximos = []
  for materia, fecha in examenes.items():
    dia = interpretar_fecha(fecha)
    if dia is not None and dia >= date.today():
      proximos.append((dia, materia))
  if not proximos:
    bot.send_message(
        message.chat.id,
        "📅 No tenés exámenes próximos con fechas válidas. Cargá uno con"
        " /agregarexamen.",
    )
    return
  dia, materia = min(proximos)
  faltan = (dia - date.today()).days
  if faltan == 0:
    texto = (
        "📅 ¡Tu próximo examen de <b>"
        f"{html.escape(materia)}</b> es hoy! ({dia.strftime('%d/%m/%Y')})"
    )
  elif faltan == 1:
    texto = (
        "📅 Falta <b>1 día</b> para tu próximo examen de <b>"
        f"{html.escape(materia)}</b> ({dia.strftime('%d/%m/%Y')})."
    )
  else:
    texto = (
        f"📅 Faltan <b>{faltan} días</b> para tu próximo examen de <b>"
        f"{html.escape(materia)}</b> ({dia.strftime('%d/%m/%Y')})."
    )
  bot.send_message(message.chat.id, texto)


@bot.message_handler(commands=["reiniciar"])
def comando_reiniciar(message):
  registrar_interaccion(message)
  teclado = types.InlineKeyboardMarkup()
  teclado.row(
      types.InlineKeyboardButton("✅ Sí, borrar", callback_data="reiniciar_si"),
      types.InlineKeyboardButton("❌ Cancelar", callback_data="reiniciar_no"),
  )
  bot.send_message(
      message.chat.id,
      "⚠️ ¿Querés borrar tus materias, horarios y exámenes cargados?",
      reply_markup=teclado,
  )


@bot.message_handler(commands=["guia"])
def comando_guia(message):
  registrar_interaccion(message)
  ruta = "guia_estudio.txt"
  if not os.path.exists(ruta):
    bot.send_message(message.chat.id, "No encontré el archivo guia_estudio.txt.")
    return
  bot.send_message(message.chat.id, "⏳ Estoy preparando tu guía de estudio...")
  bot.send_chat_action(message.chat.id, "upload_document")
  with open(ruta, "rb") as archivo:
    bot.send_document(
        message.chat.id, archivo, caption="📎 Guía rápida de estudio"
    )


@bot.message_handler(commands=["notebooklm"])
def comando_notebooklm(message):
  registrar_interaccion(message)
  botones = types.InlineKeyboardMarkup()
  botones.add(
      types.InlineKeyboardButton(
          "📖 Abrir NotebookLM", url="https://notebooklm.google.com/"
      )
  )
  bot.send_message(
      message.chat.id,
      "<b>📚 NotebookLM</b>\n\nAbrí NotebookLM para subir tus apuntes,"
      " organizar fuentes y estudiar con ellas. Tocá el botón para ir a la"
      " página oficial.\n\n<i>Es una herramienta externa: no está conectada"
      " automáticamente con los documentos ni las materias de StudyBot.</i>",
      reply_markup=botones,
  )


@bot.message_handler(commands=["ambiente"])
def comando_ambiente(message):
  registrar_interaccion(message)
  botones = types.InlineKeyboardMarkup()
  botones.add(
      types.InlineKeyboardButton(
          "🎧 Abrir espacio de estudio", url="https://lifeat.io/app?space=157"
      )
  )
  bot.send_message(
      message.chat.id,
      "<b>🎧 Ambiente de estudio — LifeAt</b>\n\nAbrí un espacio virtual para"
      " acompañar tus sesiones de estudio y ayudarte a concentrarte. Tocá el"
      " botón para entrar.\n\n<i>Es una página externa; no está integrada con"
      " StudyBot.</i>",
      reply_markup=botones,
  )


@bot.message_handler(commands=["tareas", "checklist"])
def comando_tareas(message):
  registrar_interaccion(message)
  mostrar_checklist(message.chat.id)


@bot.message_handler(commands=["agregartarea"])
def comando_agregar_tarea(message):
  registrar_interaccion(message)
  solicitar_tarea(message.chat.id)


def extraer_texto_documento(nombre, contenido):
  extension = os.path.splitext(nombre.lower())[1]
  if extension in (".txt", ".md"):
    return contenido.decode("utf-8-sig")
  if extension == ".pdf":
    from pypdf import PdfReader

    lector = PdfReader(io.BytesIO(contenido))
    return "\n".join(pagina.extract_text() or "" for pagina in lector.pages)
  if extension == ".docx":
    from docx import Document

    documento = Document(io.BytesIO(contenido))
    return "\n".join(p.text for p in documento.paragraphs)
  if extension == ".pptx":
    from pptx import Presentation

    presentacion = Presentation(io.BytesIO(contenido))
    return "\n".join(
        forma.text
        for diapositiva in presentacion.slides
        for forma in diapositiva.shapes
        if forma.has_text_frame
    )
  raise ValueError("Formato no admitido. Enviá PDF, TXT, MD, DOCX o PPTX.")


@bot.message_handler(content_types=["document"])
def recibir_documento(message):
  registrar_interaccion(message)
  materias = obtener_materias(message.chat.id)
  if not materias:
    bot.send_message(
        message.chat.id,
        "📚 Primero cargá una materia con /agregarmateria y después enviá el"
        " documento.",
    )
    return
  nombre = message.document.file_name or "documento"
  if (
      os.path.splitext(nombre.lower())[1]
      not in (".txt", ".md", ".pdf", ".docx", ".pptx")
  ):
    bot.send_message(
        message.chat.id, "📄 Puedo leer archivos PDF, TXT, MD, DOCX y PPTX."
    )
    return
  if message.document.file_size and message.document.file_size > 8_000_000:
    bot.send_message(
        message.chat.id, "⚠️ El archivo es demasiado grande (máximo 8 MB)."
    )
    return
  bot.send_message(
      message.chat.id,
      "⏳ Estoy leyendo el documento para incorporarlo a tus repasos...",
  )
  bot.send_chat_action(message.chat.id, "typing")
  try:
    info = bot.get_file(message.document.file_id)
    contenido = bot.download_file(info.file_path)
    texto = extraer_texto_documento(nombre, contenido).strip()
    if not texto:
      bot.send_message(
          message.chat.id,
          "⚠️ No encontré texto seleccionable en el archivo. Los PDF escaneados"
          " como imagen necesitan OCR.",
      )
      return
    pendientes_documentos[message.chat.id] = {
        "nombre": nombre,
        "texto": texto[:45000],
    }
    opciones = ", ".join(materias)
    respuesta = bot.send_message(
        message.chat.id,
        f"📄 Leí <b>{html.escape(nombre)}</b>. ¿A qué materia pertenece? Escribí"
        f" exactamente una de estas: <b>{html.escape(opciones)}</b>.",
    )
    bot.register_next_step_handler(respuesta, asociar_documento)
  except Exception as error:
    print("Error leyendo documento:", error)
    bot.send_message(
        message.chat.id,
        "⚠️ No pude leer ese documento. Verificá el formato y probá de nuevo.",
    )


def asociar_documento(message):
  if cancelar_si_es_comando(message):
    pendientes_documentos.pop(message.chat.id, None)
    return
  registrar_interaccion(message)
  materias = obtener_materias(message.chat.id)
  nombre = next(
      (
          n
          for n in materias
          if n.casefold() == (message.text or "").strip().casefold()
      ),
      None,
  )
  pendiente = pendientes_documentos.get(message.chat.id)
  if not pendiente:
    bot.send_message(
        message.chat.id,
        "La carga del documento expiró. Enviá el archivo nuevamente.",
    )
    return
  if nombre is None:
    respuesta = bot.send_message(
        message.chat.id,
        "⚠️ Esa materia no está cargada. Escribí una de: " + ", ".join(materias),
    )
    bot.register_next_step_handler(respuesta, asociar_documento)
    return
  documentos = documentos_por_usuario.setdefault(message.chat.id, {}).setdefault(
      nombre, []
  )
  documentos.append(pendientes_documentos.pop(message.chat.id))
  bot.send_message(
      message.chat.id,
      f"✅ Documento asociado a <b>{html.escape(nombre)}</b>. Ahora /repasar"
      " utilizará su texto como fuente.",
  )


@bot.message_handler(content_types=["voice", "audio"])
def recibir_audio(message):
  registrar_interaccion(message)
  bot.send_message(message.chat.id, "🎤 Recibí tu audio correctamente.")


@bot.message_handler(content_types=["photo"])
def recibir_foto(message):
  registrar_interaccion(message)
  bot.send_message(message.chat.id, "🖼️ Recibí tu imagen correctamente.")


@bot.message_handler(content_types=["text"])
def mensaje_de_texto(message):
  registrar_interaccion(message)
  texto = (message.text or "").strip()
  if texto.split("@")[0].lower() == "/notebooklm":
    comando_notebooklm(message)
    return
  if texto.split("@")[0].lower() == "/ambiente":
    comando_ambiente(message)
    return
  if texto.startswith("/"):
    bot.send_message(
        message.chat.id, "No conozco ese comando. Probá con /ayuda."
    )
    return
  if texto.lower() in ["hola", "buenas", "buen día", "buen dia", "hey"]:
    bot.send_message(
        message.chat.id,
        "¡Hola! 👋 ¿Qué querés hacer hoy?",
        reply_markup=menu_inline(),
    )
    return
  bot.send_message(
      message.chat.id,
      "No reconocí esa opción. Usá /ayuda para ver los comandos disponibles.",
  )


bot.set_my_commands([
    types.BotCommand("start", "Iniciar StudyBot"),
    types.BotCommand("ayuda", "Ver comandos"),
    types.BotCommand("materias", "Ver materias"),
    types.BotCommand("horarios", "Ver horarios"),
    types.BotCommand("examenes", "Ver próximos exámenes"),
    types.BotCommand("repasar", "Generar actividad de repaso"),
    types.BotCommand("metodo", "Obtener método de estudio"),
    types.BotCommand("plan", "Calcular un plan de estudio"),
    types.BotCommand("tareas", "Checklist de tareas"),
    types.BotCommand("agregartarea", "Agregar tarea al checklist"),
    types.BotCommand("agregarmateria", "Cargar una materia"),
    types.BotCommand("agregarexamen", "Cargar un examen"),
    types.BotCommand("calendario", "Ver calendario de estudio"),
    types.BotCommand("recordatorio", "Días hasta el próximo examen"),
    types.BotCommand("reiniciar", "Borrar datos académicos"),
    types.BotCommand("historial", "Ver últimas consultas"),
    types.BotCommand("guia", "Recibir guía de estudio"),
    types.BotCommand("notebooklm", "Abrir NotebookLM"),
    types.BotCommand("ambiente", "Abrir espacio de estudio LifeAt"),
    types.BotCommand("atajos", "Mostrar botones rápidos"),
    types.BotCommand("fin", "Finalizar"),
])


# --- RUTAS DE FLASK PARA EL WEBHOOK EN VERCEL (Reemplaza al polling) ---


@app.route("/api/webhook", methods=["POST"])
def webhook():
  # Validamos el secreto de Telegram por seguridad
  if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
    return "Forbidden", 403

  if request.headers.get("content-type") == "application/json":
    json_string = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "", 200
  return "Forbidden", 403


@app.route("/", methods=["GET"])
def index():
  return {"ok": True, "service": "studybot-vercel-python"}, 200