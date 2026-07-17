//+------------------------------------------------------------------+
//| TradeSignaler.mq5                                                |
//| Señaliza a CopyMT5 cuando hay actividad en la cuenta.            |
//| Adjuntar en cualquier gráfico del terminal MASTER.               |
//+------------------------------------------------------------------+
#property copyright ""
#property version   "1.00"
#property description "Escribe una señal al archivo copy_mt5_signal.txt cada vez que"
#property description "ocurre un evento de trading (apertura, cierre, modificación)."

#define SIGNAL_FILE "copy_mt5_signal.txt"

//+------------------------------------------------------------------+
void OnInit() {
    WriteSignal();
    Print("TradeSignaler activo. Señal en: ", TerminalInfoString(TERMINAL_COMMONDATA_PATH),
          "\\Files\\", SIGNAL_FILE);
}

//+------------------------------------------------------------------+
// Se dispara con CUALQUIER evento de trading en la cuenta:
// apertura de posición, cierre, modificación de SL/TP, orden pendiente.
void OnTrade() {
    WriteSignal();
}

//+------------------------------------------------------------------+
void WriteSignal() {
    int handle = FileOpen(SIGNAL_FILE, FILE_WRITE | FILE_TXT | FILE_COMMON | FILE_ANSI);
    if (handle == INVALID_HANDLE) {
        Print("TradeSignaler: no se pudo escribir la señal. Error: ", GetLastError());
        return;
    }
    FileWrite(handle, (string)TimeTradeServer());
    FileClose(handle);
}
//+------------------------------------------------------------------+
