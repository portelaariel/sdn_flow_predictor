import pandas as pd
import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.metrics import accuracy_score, classification_report, root_mean_squared_error
import pickle
import os

CSV_FILE = 'dataset_cnsm2025_limpo.csv'
MODELO_CLASSIFICACAO_FILE = 'modelo_anomalia.pkl'
MODELO_REGRESSAO_FILE = 'modelo_vazao.pkl'
PASSOS_FUTURO = 10 # 10 segundos à frente (assumindo que a coleta foi de 1 em 1 segundo)

def main():
    if not os.path.exists(CSV_FILE):
        print(f"Erro: Arquivo {CSV_FILE} não encontrado. Rode o coletor primeiro.")
        return

    print("1. Carregando os dados...")
    df = pd.read_csv(CSV_FILE)

    # Verifica se há dados suficientes
    if len(df) <= PASSOS_FUTURO:
        print("Erro: Dados insuficientes no CSV. Colete dados por mais tempo.")
        return

    print("2. Pré-processando os dados (Time Shifting)...")
    # Para prever o futuro, precisamos alinhar os dados atuais com a resposta de 10 segundos no futuro.
    # Fazemos um agrupamento por porta (dpid_port) para não misturar portas diferentes
    df['dpid_port'] = df['dpid'].astype(str) + "_" + df['port_no'].astype(str)
    
    # Criamos as colunas "Alvo" (Target) deslocando os dados 10 linhas para cima (-10)
    df['target_anomalia'] = df.groupby('dpid_port')['anomalia'].shift(-PASSOS_FUTURO)
    df['target_tx_vazao'] = df.groupby('dpid_port')['tx_vazao_bps'].shift(-PASSOS_FUTURO)
    
    # Ao fazer o shift, as últimas 10 linhas ficarão sem alvo (NaN). Precisamos removê-las.
    df = df.dropna(subset=['target_anomalia', 'target_tx_vazao'])

    # Definimos quais colunas o modelo vai usar para "olhar" (Features)
    features = ['rx_bytes', 'tx_bytes', 'rx_vazao_bps', 'tx_vazao_bps']
    X = df[features]
    
    # Alvos (Targets)
    y_classificacao = df['target_anomalia'].astype(int)
    y_regressao = df['target_tx_vazao']

    print("3. Dividindo em dados de Treino (80%) e Teste (20%)...")
    # Não vamos embaralhar (shuffle=False) para manter a ordem cronológica da série temporal
    X_train, X_test, yc_train, yc_test, yr_train, yr_test = train_test_split(
        X, y_classificacao, y_regressao, test_size=0.2, shuffle=False
    )

    print("4. Treinando modelo de Classificação (Vai ser Anomalia ou não?)...")
    clf = RandomForestClassifier(n_estimators=50, random_state=42)
    clf.fit(X_train, yc_train)
    
    # Avaliando a classificação
    yc_pred = clf.predict(X_test)
    print("\n--- Relatório de Classificação de Anomalia (10s à frente) ---")
    print(classification_report(yc_test, yc_pred, zero_division=0))

    print("5. Treinando modelo de Regressão (Qual será a vazão exata?)...")
    reg = RandomForestRegressor(n_estimators=50, random_state=42)
    reg.fit(X_train, yr_train)
    
    # Avaliando a regressão com RMSE
    yr_pred = reg.predict(X_test)
    rmse = root_mean_squared_error(yr_test, yr_pred)
    print(f"\n--- Erro da Predição de Vazão (RMSE) ---")
    print(f"O modelo erra a vazão em média por: {rmse:.2f} bps")

    print("\n6. Salvando os modelos treinados...")
    with open(MODELO_CLASSIFICACAO_FILE, 'wb') as f:
        pickle.dump(clf, f)
    with open(MODELO_REGRESSAO_FILE, 'wb') as f:
        pickle.dump(reg, f)
        
    print(f"Concluído! Modelos salvos como '{MODELO_CLASSIFICACAO_FILE}' e '{MODELO_REGRESSAO_FILE}'.")

if __name__ == "__main__":
    main()