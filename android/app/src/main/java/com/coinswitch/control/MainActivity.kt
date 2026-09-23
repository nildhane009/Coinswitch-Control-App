package com.coinswitch.control
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

class MainActivity:ComponentActivity(){
 override fun onCreate(b:Bundle?){super.onCreate(b);setContent{App()}}
 @Composable fun App(){
  var url by remember{mutableStateOf("http://127.0.0.1:8787")}
  var response by remember{mutableStateOf("Connecting…")}
  var orders by remember{mutableStateOf(false)}
  var paper by remember{mutableStateOf(false)}
  var live by remember{mutableStateOf(false)}
  var low by remember{mutableStateOf(false)}
  var trailing by remember{mutableStateOf(true)}
  fun req(path:String,body:String?=null){
   Executors.newSingleThreadExecutor().execute{
    try{
     val c=URL(url.trimEnd('/')+path).openConnection() as HttpURLConnection
     c.requestMethod=if(body==null)"GET" else "POST"; c.connectTimeout=2500;c.readTimeout=3500
     c.setRequestProperty("Content-Type","application/json")
     if(body!=null){c.doOutput=true;c.outputStream.use{it.write(body.toByteArray())}}
     val r=c.inputStream.bufferedReader().readText()
     runOnUiThread{response=r}
     c.disconnect()
    }catch(e:Exception){runOnUiThread{response="Disconnected: ${e.message}"}}
   }
  }
  LaunchedEffect(Unit){while(true){req("/api/status");kotlinx.coroutines.delay(3000)}}
  MaterialTheme{Scaffold(topBar={TopAppBar(title={Text("CoinSwitch Control",fontWeight=FontWeight.Bold)})}){p->
   LazyColumn(Modifier.fillMaxSize().padding(p).padding(16.dp),verticalArrangement=Arrangement.spacedBy(12.dp)){
    item{OutlinedTextField(url,{url=it},Modifier.fillMaxWidth(),label={Text("Termux Bridge URL")},singleLine=true)}
    item{Card(Modifier.fillMaxWidth()){Column(Modifier.padding(16.dp)){Text(if(response.startsWith("Disconnected"))"● Disconnected" else "● Bridge response",fontWeight=FontWeight.Bold);Button({req("/api/status")}){Text("Refresh")}}}}
    item{Card(Modifier.fillMaxWidth()){Column(Modifier.padding(16.dp)){
     Text("Bot Controls",fontWeight=FontWeight.Bold)
     Toggle("Master Orders",orders){orders=it;req("/api/control/master","""{"enabled":$it}""")}
     Toggle("Paper Mode",paper){paper=it;if(it)live=false;req("/api/control/mode","""{"mode":"${if(it)"PAPER" else "OFF"}"}""")}
     Toggle("Live Mode",live){live=it;if(it)paper=false;req("/api/control/mode","""{"mode":"${if(it)"LIVE" else "OFF"}"}""")}
     Toggle("Low Capital Mode",low){low=it;req("/api/control/low-capital","""{"enabled":$it}""")}
     Toggle("Trailing Stop",trailing){trailing=it;req("/api/control/trailing","""{"enabled":$it}""")}
    }}}
    item{Button({req("/api/recovery")},Modifier.fillMaxWidth()){Text("LIVE RECOVERY / SYNC")}}
    item{Button({req("/api/emergency-exit-all","""{"confirm":true}""")},Modifier.fillMaxWidth(),colors=ButtonDefaults.buttonColors(containerColor=MaterialTheme.colorScheme.error)){Text("EMERGENCY EXIT ALL")}}
    item{Text("Live State",style=MaterialTheme.typography.titleMedium);Text(response)}
   }
  }}
 }
 @Composable fun Toggle(label:String,value:Boolean,on:(Boolean)->Unit){Row(Modifier.fillMaxWidth(),verticalAlignment=Alignment.CenterVertically,horizontalArrangement=Arrangement.SpaceBetween){Text(label);Switch(value,on)}}
}
