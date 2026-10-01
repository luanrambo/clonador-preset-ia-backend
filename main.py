from fastapi import FastAPI, UploadFile, File, Header, HTTPException, Request
from fastapi.responses import Response
from supabase import create_client, Client
import cv2
import numpy as np
import os
import tempfile
import uuid

# --- CONFIGURAÇÕES DO SUPABASE (Puxa do Servidor Seguro) ---
SUPABASE_URL = os.getenv("SUPABASE_URL", "SUA_URL_AQUI")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "SUA_CHAVE_SERVICE_ROLE_AQUI")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="Clonador de Preset IA - Engine API", version="1.0")

def processar_bytes(conteudo_bytes, nome_ficheiro):
    extensao = os.path.splitext(nome_ficheiro)[1].lower()
    extensoes_raw = ['.cr2', '.cr3', '.nef', '.arw', '.dng', '.raf', '.orf', '.rw2']
    
    if extensao in extensoes_raw:
        import rawpy
        with tempfile.NamedTemporaryFile(delete=False, suffix=extensao) as temp_file:
            temp_file.write(conteudo_bytes)
            temp_path = temp_file.name
        try:
            with rawpy.imread(temp_path) as raw:
                rgb = raw.postprocess(use_camera_wb=True, half_size=False, no_auto_bright=True)
                img_bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            os.remove(temp_path)
            return img_bgr, True
        except Exception as e:
            if os.path.exists(temp_path): os.remove(temp_path)
            raise e

    np_arr = np.frombuffer(conteudo_bytes, np.uint8)
    return cv2.imdecode(np_arr, cv2.IMREAD_COLOR), False

# --- ROTA 1: WEBHOOK DA KIWIFY (RECEBE PAGAMENTO E LIBERA CRÉDITOS) ---
@app.post("/webhook/kiwify")
async def webhook_kiwify(request: Request):
    dados = await request.json()
    status_pedido = dados.get("order_status") or dados.get("status")
    
    if status_pedido in ["paid", "approved"]:
        email_cliente = dados.get("Customer", {}).get("email") or dados.get("email")
        
        if email_cliente:
            email_cliente = email_cliente.lower().strip()
            res = supabase.table("profiles").select("*").eq("email", email_cliente).execute()
            
            if len(res.data) > 0:
                creditos_atuais = res.data[0]["creditos"]
                supabase.table("profiles").update({"creditos": creditos_atuais + 100}).eq("email", email_cliente).execute()
            else:
                supabase.table("profiles").insert({
                    "email": email_cliente,
                    "creditos": 100,
                    "status_assinatura": "active"
                }).execute()
                
            return {"status": "sucesso", "mensagem": f"100 créditos liberados para {email_cliente}"}
            
    return {"status": "ignorado", "mensagem": "Pagamento não aprovado."}

# --- ROTA 2: CLONADOR DE COR (PRODUTO PRINCIPAL) ---
@app.post("/api/clonar-cor/")
async def clonar_cor(
    user_email: str = Header(..., alias="X-User-Email"),
    foto_referencia: UploadFile = File(...),
    foto_alvo: UploadFile = File(...)
):
    # 1. Checa se o usuário tem créditos
    res = supabase.table("profiles").select("creditos").eq("email", user_email.lower().strip()).execute()
    if len(res.data) == 0 or res.data[0]["creditos"] <= 0:
        raise HTTPException(status_code=403, detail="Créditos insuficientes. Adquira mais acessos.")
    
    # 2. Lê os arquivos
    bytes_ref = await foto_referencia.read()
    bytes_alvo = await foto_alvo.read()
    
    img_ref, _ = processar_bytes(bytes_ref, foto_referencia.filename)
    img_alvo, is_raw = processar_bytes(bytes_alvo, foto_alvo.filename)
    
    # 3. Faz a matemática de cor no LAB Color Space
    l_ref, a_ref, b_ref = cv2.split(cv2.cvtColor(img_ref, cv2.COLOR_BGR2LAB))
    l_alvo, a_alvo, b_alvo = cv2.split(cv2.cvtColor(img_alvo, cv2.COLOR_BGR2LAB))
    
    diff_brilho = np.mean(l_ref) - np.mean(l_alvo)
    exposicao = float(np.clip(diff_brilho / 65.0, -2.0, 2.0))
    
    diff_temp = (np.mean(b_ref) - np.mean(b_alvo)) * 1.2
    temperatura = int(5500 + np.tanh(diff_temp / 400.0) * 400) if is_raw else int(np.tanh(diff_temp / 12.0) * 12)
    
    # 4. Gera o arquivo final com ID único para o Lightroom
    codigo_id = str(uuid.uuid4())[:6].upper()
    xmp_xml = f"""<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
    crs:Temperature="{temperatura}" crs:Exposure2012="{exposicao:.2f}" crs:HasSettings="True">
   <crs:Name><rdf:Alt><rdf:li xml:lang="x-default">Clonador Preset IA [{codigo_id}]</rdf:li></rdf:Alt></crs:Name>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>"""

    # 5. Desconta 1 crédito do banco de dados
    supabase.table("profiles").update({"creditos": res.data[0]["creditos"] - 1}).eq("email", user_email.lower().strip()).execute()

    return Response(
        content=xmp_xml,
        media_type="application/xml",
        headers={"Content-Disposition": f"attachment; filename=Preset_IA_{codigo_id}.xmp"}
    )