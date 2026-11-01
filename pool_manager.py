"""
========================================================================================
DEFI SENTINEL & ALM MANAGER (ARBITRUM UNISWAP V3 + HYPERLIQUID DELTA-NEUTRAL HEDGE)
Author: Douglas Guanaes (Quantitative Software & DeFi Systems)
Architecture: Automated Liquidity Management (ALM) + Cross-Protocol Flash-Crash Shield
Target: Arbitrum One (L2) | Pair: WBTC / WETH (Pool 0.05%) | Hedge: Hyperliquid DEX
========================================================================================
"""
import os
import sys
import json
import time
import logging
import math
from collections import deque
from datetime import datetime, timezone
from web3 import Web3

# Força codificação UTF-8 no terminal
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# Configuração de Logs
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("pool_manager.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)

def log(msg):
    logging.info(msg)

# ==============================================================================
# CARREGAMENTO SEGURO DE CONFIGURAÇÕES (VIA VARIÁVEIS DE AMBIENTE)
# ==============================================================================
# Modo de Execução: False = Monitoramento e Fogo Real | True = Paper Trading
MODO_PAPER_POOL = os.getenv("MODO_PAPER_POOL", "True").lower() in ("true", "1", "yes")

# Dados Operacionais (Sanitizados - Nenhuma chave ou endereço pessoal exposto)
CARTEIRA_OPERACIONAL = os.getenv("CARTEIRA_OPERACIONAL", "0x0000000000000000000000000000000000000000")
NFT_ID_POSICAO = int(os.getenv("NFT_ID_POSICAO", "0"))
CAPITAL_ALOCADO_USD = float(os.getenv("CAPITAL_ALOCADO_USD", "1000.00"))

# Parâmetros de Faixa Inicial da Uniswap v3 (Ratio WETH/WBTC)
TICK_MIN_CONFIG = int(os.getenv("TICK_MIN_CONFIG", "264620"))
TICK_MAX_CONFIG = int(os.getenv("TICK_MAX_CONFIG", "266620"))
FAIXA_MIN_RATIO = float(os.getenv("FAIXA_MIN_RATIO", "31.00"))
FAIXA_MAX_RATIO = float(os.getenv("FAIXA_MAX_RATIO", "38.00"))

# Gatilhos de Proteção do Sentinela
QUEDA_DISPARO_HEDGE_PCT = float(os.getenv("QUEDA_DISPARO_HEDGE_PCT", "0.035")) # 3.5% de queda
INTERVALO_SENTINELA_SEG = int(os.getenv("INTERVALO_SENTINELA_SEG", "5"))        # Checagem a cada 5s
AMOSTRAS_JANELA_2MIN = 24                                                      # 24 * 5s = 120s

ARQUIVO_ESTADO_POOL = "estado_pool_arbitrum.json"

# ==============================================================================
# INFRAESTRUTURA WEB3 (REDUNDÂNCIA TRIPLA DE RPC ARBITRUM)
# ==============================================================================
RPCS_ARBITRUM = [
    os.getenv("ARBITRUM_RPC_1", "https://arb1.arbitrum.io/rpc"),
    os.getenv("ARBITRUM_RPC_2", "https://1rpc.io/arb"),
    os.getenv("ARBITRUM_RPC_3", "https://arbitrum-one.publicnode.com")
]

def obter_provedor_web3():
    """Gera instâncias Web3 com timeout ágil e failover automático de RPC."""
    for rpc in RPCS_ARBITRUM:
        try:
            provider = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 5}))
            if provider.is_connected():
                return provider
        except Exception:
            continue
    return Web3(Web3.HTTPProvider(RPCS_ARBITRUM[0]))

w3 = obter_provedor_web3()

# Contratos Oficiais na Arbitrum One
POOL_WBTC_WETH_005 = "0x2f5e87C9312fa29aed5c179E456625D79015299c"
POSITION_MANAGER_ADDR = "0xC36442b4a4522E871399CD717aBDD847Ab11FE88"

# ABIs Mínimas (apenas os métodos necessários para leitura eficiente)
POOL_ABI = [
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
            {"internalType": "int24", "name": "tick", "type": "int24"},
            {"internalType": "uint16", "name": "observationIndex", "type": "uint16"},
            {"internalType": "uint16", "name": "observationCardinality", "type": "uint16"},
            {"internalType": "uint16", "name": "observationCardinalityNext", "type": "uint16"},
            {"internalType": "uint8", "name": "feeProtocol", "type": "uint8"},
            {"internalType": "bool", "name": "unlocked", "type": "bool"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "liquidity",
        "outputs": [{"internalType": "uint128", "name": "", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function"
    }
]

POSITION_MANAGER_ABI = [
    {
        "inputs": [{"internalType": "uint256", "name": "tokenId", "type": "uint256"}],
        "name": "positions",
        "outputs": [
            {"internalType": "uint96", "name": "nonce", "type": "uint96"},
            {"internalType": "address", "name": "operator", "type": "address"},
            {"internalType": "address", "name": "token0", "type": "address"},
            {"internalType": "address", "name": "token1", "type": "address"},
            {"internalType": "uint24", "name": "fee", "type": "uint24"},
            {"internalType": "int24", "name": "tickLower", "type": "int24"},
            {"internalType": "int24", "name": "tickUpper", "type": "int24"},
            {"internalType": "uint128", "name": "liquidity", "type": "uint128"},
            {"internalType": "uint256", "name": "feeGrowthInside0LastX128", "type": "uint256"},
            {"internalType": "uint256", "name": "feeGrowthInside1LastX128", "type": "uint256"},
            {"internalType": "uint128", "name": "tokensOwed0", "type": "uint128"},
            {"internalType": "uint128", "name": "tokensOwed1", "type": "uint128"}
        ],
        "stateMutability": "view",
        "type": "function"
    }
]

# ==============================================================================
# GESTÃO DE ESTADO & PERSISTÊNCIA ATÔMICA
# ==============================================================================
def carregar_estado_pool():
    if os.path.exists(ARQUIVO_ESTADO_POOL):
        try:
            with open(ARQUIVO_ESTADO_POOL, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            log(f"⚠️ Erro ao ler estado existente: {e}")
            
    return {
        "capital_usd": CAPITAL_ALOCADO_USD,
        "em_pool": True,
        "nft_id": NFT_ID_POSICAO,
        "tick_lower": TICK_MIN_CONFIG,
        "tick_upper": TICK_MAX_CONFIG,
        "faixa_min_ratio": FAIXA_MIN_RATIO,
        "faixa_max_ratio": FAIXA_MAX_RATIO,
        "taxas_acumuladas_usd": 0.0,
        "hedge_ativo": False,
        "preco_ultimo_check": 0.0,
        "carteira": CARTEIRA_OPERACIONAL,
        "historico_eventos": []
    }

def salvar_estado_pool(estado):
    try:
        temp = f"{ARQUIVO_ESTADO_POOL}.tmp"
        with open(temp, 'w', encoding='utf-8') as f:
            json.dump(estado, f, indent=4)
        os.replace(temp, ARQUIVO_ESTADO_POOL)
    except Exception as e:
        log(f"⚠️ Erro ao salvar estado da pool: {e}")

# ==============================================================================
# LEITURA ON-CHAIN EM TEMPO REAL (UNISWAP V3)
# ==============================================================================
def obter_preco_pool_wbtc_weth():
    """Lê slot0 e extrai sqrtPriceX96 e tick com failover de RPC."""
    global w3
    for rpc in RPCS_ARBITRUM:
        try:
            pool_contract = w3.eth.contract(address=Web3.to_checksum_address(POOL_WBTC_WETH_005), abi=POOL_ABI)
            slot0 = pool_contract.functions.slot0().call()
            sqrtPriceX96 = slot0[0]
            tick = slot0[1]
            
            # WBTC: 8 decimais | WETH: 18 decimais
            preco_ratio = ((sqrtPriceX96 / (2**96)) ** 2) * (10**8 / 10**18)
            weth_por_btc = 1.0 / preco_ratio if preco_ratio > 0 else 0.0
            if preco_ratio < 0.1:
                weth_por_btc = 1.0 / preco_ratio
            else:
                weth_por_btc = preco_ratio
            
            return {
                "tick": tick,
                "ratio_wbtc_weth": round(weth_por_btc, 2),
                "timestamp": int(time.time())
            }
        except Exception:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 5}))
            except Exception:
                pass
    
    log("⚠️ Alerta: Todos os RPCs da Arbitrum falharam no ciclo atual.")
    return None

def consultar_dados_nft_posicao(nft_id):
    """Consulta dados de liquidez e taxas acumuladas direto do NonfungiblePositionManager."""
    if not nft_id:
        return None
        
    global w3
    for rpc in RPCS_ARBITRUM:
        try:
            pm = w3.eth.contract(address=Web3.to_checksum_address(POSITION_MANAGER_ADDR), abi=POSITION_MANAGER_ABI)
            pos = pm.functions.positions(nft_id).call()
            return {
                "token0": pos[2],
                "token1": pos[3],
                "fee": pos[4],
                "tickLower": pos[5],
                "tickUpper": pos[6],
                "liquidity": pos[7],
                "tokensOwed0": pos[10],
                "tokensOwed1": pos[11]
            }
        except Exception:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 5}))
            except Exception:
                pass
    return None

# ==============================================================================
# MODELAGEM MATEMÁTICA & VOLATILIDADE DINÂMICA
# ==============================================================================
def calcular_volatilidade_dinamica(historico_precos):
    """
    Calcula desvio padrão percentual em janelas móveis:
    - Baixa volatilidade: Faixa estreita (+- 5% a 7%) para maximizar fees.
    - Alta volatilidade: Faixa elástica alargada (+- 10% a 14%) para segurança contra impermanent loss.
    """
    if len(historico_precos) < 10:
        return 0.10
        
    retornos = []
    lista = list(historico_precos)
    for i in range(1, len(lista)):
        if lista[i-1] > 0:
            retornos.append((lista[i] - lista[i-1]) / lista[i-1])
            
    if not retornos:
        return 0.10
        
    media = sum(retornos) / len(retornos)
    variancia = sum((r - media) ** 2 for r in retornos) / len(retornos)
    desvio_padrao = math.sqrt(variancia)
    
    # Escala para projeção operacional
    vol_escalada = desvio_padrao * math.sqrt(720)
    largura_otima = min(max(vol_escalada * 3.0, 0.05), 0.14)
    return round(largura_otima, 4)

def calcular_faixa_otima_concentrada(preco_ratio, largura_pct=None, historico_precos=None):
    if largura_pct is None:
        if historico_precos and len(historico_precos) >= 10:
            largura_pct = calcular_volatilidade_dinamica(historico_precos)
        else:
            largura_pct = 0.10
            
    min_ratio = preco_ratio * (1.0 - largura_pct)
    max_ratio = preco_ratio * (1.0 + largura_pct)
    return min_ratio, max_ratio, largura_pct

# ==============================================================================
# MECANISMO DE COBERTURA: HEDGE RELÂMPAGO (HYPERLIQUID)
# ==============================================================================
def disparar_hedge_relampago(motivo, estado):
    """Aciona cobertura Short de perpétuos na Hyperliquid para cravar Delta Neutro."""
    log(f"🚨 [GATILHO DE HEDGE DISPARADO] Motivo: {motivo}")

    # Notificação via Telegram
    try:
        import alerta_telegram
        alerta_telegram.avisar_comandante(
            f"🚨 <b>[ALERTA SENTINELA ANTI-CRASH]</b>\n"
            f"⚡ Gatilho disparado na Arbitrum!\n"
            f"📉 Motivo: <code>{motivo}</code>\n"
            f"🛡️ Acionando cobertura Short na Hyperliquid..."
        )
    except Exception:
        pass

    if MODO_PAPER_POOL:
        log(f"🛡️ [PAPER HEDGE] Short simulado de cobertura armado (Delta Neutro).")
        estado["hedge_ativo"] = True
        estado["historico_eventos"].append({
            "tipo": "HEDGE_ATIVADO_SIMULADO",
            "motivo": motivo,
            "ts": datetime.now(timezone.utc).isoformat()
        })
        salvar_estado_pool(estado)
    else:
        # Fogo Real com API Hyperliquid
        try:
            import hyperliquid_api
            import gestao_risco
            
            metade_nocional = CAPITAL_ALOCADO_USD / 2.0
            px_btc, px_eth = 85000.0, 2400.0
            
            # Dimensionamento via módulo institucional de risco
            lote_btc = gestao_risco.calcular_lote_hedge_pool('BTC', metade_nocional, px_btc)
            lote_eth = gestao_risco.calcular_lote_hedge_pool('ETH', metade_nocional, px_eth)
            
            sucesso_btc = hyperliquid_api.executar_ordem_mercado('BTC', 'SHORT', lote_btc) if lote_btc > 0 else False
            sucesso_eth = hyperliquid_api.executar_ordem_mercado('ETH', 'SHORT', lote_eth) if lote_eth > 0 else False
            
            if sucesso_btc or sucesso_eth:
                estado["hedge_ativo"] = True
                estado["historico_eventos"].append({
                    "tipo": "HEDGE_ATIVADO_REAL",
                    "motivo": motivo,
                    "lote_btc": lote_btc,
                    "lote_eth": lote_eth,
                    "ts": datetime.now(timezone.utc).isoformat()
                })
                salvar_estado_pool(estado)
                log("✅ [ESCUDO ATIVADO] Posição hedgada com sucesso na Hyperliquid! Delta Zero estabelecido.")
            else:
                log("❌ [FALHA DE DISPARO] Rejeição de ordem de cobertura pela DEX.")
        except Exception as e:
            log(f"❌ Falha crítica ao processar hedge na Hyperliquid: {e}")

# ==============================================================================
# CICLO PRINCIPAL (FAST-PATH SENTINELA + SLOW-PATH COGNITIVO)
# ==============================================================================
def rodar_sentinela_e_gestor():
    log("="*65)
    log("🚀 INICIANDO SENTINELA ANTI-CRASH & GESTOR DE POOL NA ARBITRUM")
    log(f"   Modo: {'PAPER TRADING' if MODO_PAPER_POOL else 'FOGO REAL (MAINNET)'}")
    log(f"   Contrato Pool: {POOL_WBTC_WETH_005}")
    log(f"   Faixa Configurada: [{FAIXA_MIN_RATIO:.2f} até {FAIXA_MAX_RATIO:.2f}] WETH/WBTC")
    log("="*65)
    
    estado = carregar_estado_pool()
    info_inicial = obter_preco_pool_wbtc_weth()
    
    if info_inicial:
        ratio = info_inicial["ratio_wbtc_weth"]
        tick_atual = info_inicial["tick"]
        estado["preco_ultimo_check"] = ratio
        salvar_estado_pool(estado)
        log(f"📊 [CONTRATO LIDO] Preço Atual: 1 WBTC = {ratio:.2f} WETH | Tick Atual: {tick_atual}")
        
        in_range = FAIXA_MIN_RATIO <= ratio <= FAIXA_MAX_RATIO
        log(f"🎯 Status da Posição: {'🟢 NO INTERVALO (GERANDO TAXAS)' if in_range else '🔴 FORA DA FAIXA'}")
    
    contador_slow_path = 0
    janela_precos_2min = deque(maxlen=AMOSTRAS_JANELA_2MIN)
    if info_inicial:
        janela_precos_2min.append(info_inicial["ratio_wbtc_weth"])
        
    while True:
        try:
            time.sleep(INTERVALO_SENTINELA_SEG)
            
            # --- 1. FAST PATH (SENTINELA VELOZ A CADA 5s) ---
            dados = obter_preco_pool_wbtc_weth()
            if not dados:
                continue
                
            ratio_atual = dados["ratio_wbtc_weth"]
            p_ant = estado.get("preco_ultimo_check", ratio_atual)
            
            janela_precos_2min.append(ratio_atual)
            p_max_2min = max(janela_precos_2min)
            
            # Queda de 1 tick (5s) e queda acumulada em 2 min
            variacao_5s = (ratio_atual - p_ant) / p_ant if p_ant > 0 else 0.0
            variacao_2min = (ratio_atual - p_max_2min) / p_max_2min if p_max_2min > 0 else 0.0
            
            gatilho_5s = variacao_5s <= -QUEDA_DISPARO_HEDGE_PCT
            gatilho_2min = variacao_2min <= -QUEDA_DISPARO_HEDGE_PCT
            
            if (gatilho_5s or gatilho_2min) and not estado.get("hedge_ativo", False):
                motivo = f"Flash crash ({variacao_5s*100:.2f}% em 5s)" if gatilho_5s else f"Cascata de liquidação ({variacao_2min*100:.2f}% em 2min)"
                disparar_hedge_relampago(motivo, estado)
                
            estado["preco_ultimo_check"] = ratio_atual
            
            # --- 2. SLOW PATH (GESTOR COGNITIVO A CADA 1H) ---
            contador_slow_path += 1
            if contador_slow_path >= 720: # 720 * 5s = 1 hora
                contador_slow_path = 0
                log("\n" + "-"*50)
                log("⏰ [GESTOR COGNITIVO - REVISÃO DINÂMICA DE VOLATILIDADE]")
                largura_dinamica = calcular_volatilidade_dinamica(janela_precos_2min)
                log(f"📊 Ratio Atual: {ratio_atual:.2f} WETH | Faixa Vigente: [{estado['faixa_min_ratio']:.2f} - {estado['faixa_max_ratio']:.2f}]")
                log(f"📈 Volatilidade Projetada: Faixa Ideal ±{largura_dinamica*100:.2f}%")
                
                if ratio_atual < estado["faixa_min_ratio"] or ratio_atual > estado["faixa_max_ratio"]:
                    log("🚨 [ALERTA] Preço saiu do intervalo. Rebalanceamento recomendado.")
                    novo_min, novo_max, larg = calcular_faixa_otima_concentrada(ratio_atual, historico_precos=janela_precos_2min)
                    estado["faixa_min_ratio"] = novo_min
                    estado["faixa_max_ratio"] = novo_max
                    log(f"🔄 Faixa Elástica Atualizada: [{novo_min:.2f} - {novo_max:.2f}] WETH")
                salvar_estado_pool(estado)
                log("-"*50 + "\n")
                
        except KeyboardInterrupt:
            log("🛑 Sentinela finalizado pelo operador.")
            break
        except Exception as e:
            log(f"⚠️ Erro recuperado no loop: {e}")
            time.sleep(5)

if __name__ == "__main__":
    rodar_sentinela_e_gestor()
