package com.phonehseng.atlas.tv

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Color
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.KeyEvent
import android.view.View
import android.view.WindowManager
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL
import java.net.URLEncoder

/**
 * Atlas on Android TV: one full-screen WebView showing the TV page that Atlas Mobile serves from the PC (/tv).
 * First run (or "Change PC") shows a small setup page from the app's assets: PCs found on the home network over
 * mDNS (_atlas._tcp), or an address typed in. Pairing itself happens in the TV page (a short code), which leaves
 * this TV with its own cookie. The remote's Back goes to the page first (window.atlasBack), OK is Enter.
 */
class MainActivity : Activity() {
    private lateinit var web: WebView
    private val main = Handler(Looper.getMainLooper())
    private val prefs by lazy { getSharedPreferences("atlas", Context.MODE_PRIVATE) }
    private var nsd: NsdManager? = null
    private var discovery: NsdManager.DiscoveryListener? = null
    private var multicast: WifiManager.MulticastLock? = null
    private val found = LinkedHashMap<String, JSONObject>()
    private val resolving = ArrayDeque<NsdServiceInfo>()
    private var resolveBusy = false

    @SuppressLint("SetJavaScriptEnabled", "AddJavascriptInterface")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        immersive()
        web = WebView(this)
        web.setBackgroundColor(Color.parseColor("#070C1D"))
        web.setLayerType(View.LAYER_TYPE_HARDWARE, null)
        web.isFocusable = true
        web.isFocusableInTouchMode = true
        with(web.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            useWideViewPort = true
            loadWithOverviewMode = true
            setSupportZoom(false)
            builtInZoomControls = false
            displayZoomControls = false
            cacheMode = WebSettings.LOAD_DEFAULT
            mixedContentMode = WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE
            userAgentString = "$userAgentString AtlasTV/${BuildConfigVersion.name(this@MainActivity)}"
        }
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false)
        web.addJavascriptInterface(Bridge(), "AtlasNative")
        web.webViewClient = Client()
        web.webChromeClient = object : WebChromeClient() {
            // no grey "play" placeholder before a video's first frame
            override fun getDefaultVideoPoster(): Bitmap = Bitmap.createBitmap(1, 1, Bitmap.Config.ARGB_8888)
        }
        setContentView(web)
        web.requestFocus()
        val saved = savedUrl()
        if (saved == null) showSetup(null) else web.loadUrl("$saved/r/tv")
    }

    // ------------------------------------------------------------------ pages
    private fun savedUrl(): String? = prefs.getString("url", null)?.takeIf { it.isNotBlank() }

    private fun showSetup(error: String?) {
        val q = StringBuilder("file:///android_asset/setup.html?")
        savedUrl()?.let { q.append("addr=").append(URLEncoder.encode(it, "UTF-8")).append('&') }
        error?.let { q.append("error=").append(URLEncoder.encode(it, "UTF-8")) }
        web.loadUrl(q.toString())
    }

    private fun onSetupPage() = web.url?.startsWith("file:///android_asset/") == true

    private inner class Client : WebViewClient() {
        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            val u = request.url
            val base = savedUrl() ?: return false
            return u.scheme == "file" || !(u.toString().startsWith(base))   // stay on the PC's pages
        }

        @Deprecated("Deprecated in Java")
        override fun shouldOverrideUrlLoading(view: WebView, url: String): Boolean {
            val base = savedUrl() ?: return false
            return !url.startsWith(base)
        }

        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
            if (Build.VERSION.SDK_INT >= 23 && request.isForMainFrame && !request.url.toString().startsWith("file:")) {
                showSetup("Can't reach Atlas at ${savedUrl() ?: request.url}. Is the PC on, with Atlas Mobile turned on?")
            }
        }

        @Deprecated("Deprecated in Java")
        override fun onReceivedError(view: WebView, errorCode: Int, description: String?, failingUrl: String?) {
            if (Build.VERSION.SDK_INT < 23 && failingUrl != null && !failingUrl.startsWith("file:")) {
                showSetup("Can't reach Atlas at ${savedUrl() ?: failingUrl}. Is the PC on, with Atlas Mobile turned on?")
            }
        }
    }

    // ------------------------------------------------------------------ the remote
    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        when (event.keyCode) {
            KeyEvent.KEYCODE_BACK, KeyEvent.KEYCODE_ESCAPE -> {
                if (event.action == KeyEvent.ACTION_UP && !event.isCanceled) back()
                return true
            }
            KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_BUTTON_A, KeyEvent.KEYCODE_NUMPAD_ENTER -> {
                // OK on the remote is Enter for the page (some WebViews don't map it)
                return web.dispatchKeyEvent(KeyEvent(event.downTime, event.eventTime, event.action,
                    KeyEvent.KEYCODE_ENTER, event.repeatCount, event.metaState))
            }
        }
        return super.dispatchKeyEvent(event)
    }

    private fun back() {
        if (onSetupPage()) {
            val saved = savedUrl()
            if (saved != null && web.url?.contains("error=") != true && web.url?.contains("change=1") == true) web.loadUrl("$saved/r/tv")
            else moveTaskToBack(true)
            return
        }
        web.evaluateJavascript("(window.atlasBack && window.atlasBack()) ? '1' : '0'") { r ->
            if (r == null || !r.contains("1")) moveTaskToBack(true)
        }
    }

    // ------------------------------------------------------------------ lifecycle
    override fun onPause() {
        web.evaluateJavascript("window.atlasPause && window.atlasPause()", null)
        web.onPause()
        CookieManager.getInstance().flush()
        stopDiscovery()
        super.onPause()
    }

    override fun onResume() {
        super.onResume()
        web.onResume()
        immersive()
    }

    override fun onDestroy() {
        stopDiscovery()
        web.destroy()
        super.onDestroy()
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) immersive()
    }

    @Suppress("DEPRECATION")
    private fun immersive() {
        window.decorView.systemUiVisibility = (View.SYSTEM_UI_FLAG_LAYOUT_STABLE or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
            or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
            or View.SYSTEM_UI_FLAG_FULLSCREEN or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY)
    }

    // ------------------------------------------------------------------ finding the PC (mDNS)
    private fun startDiscovery() {
        stopDiscovery()
        found.clear()
        val wifi = applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
        multicast = wifi?.createMulticastLock("atlas-discovery")?.apply { setReferenceCounted(false); acquire() }
        val mgr = getSystemService(Context.NSD_SERVICE) as? NsdManager ?: return
        nsd = mgr
        val listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {}
            override fun onDiscoveryStopped(serviceType: String) {}
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) { main.post { stopDiscovery() } }
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
            override fun onServiceLost(info: NsdServiceInfo) {}
            override fun onServiceFound(info: NsdServiceInfo) {
                main.post { resolving.addLast(info); resolveNext() }
            }
        }
        discovery = listener
        try {
            mgr.discoverServices("_atlas._tcp.", NsdManager.PROTOCOL_DNS_SD, listener)
        } catch (e: Exception) {
            discovery = null
        }
        main.postDelayed({ stopDiscovery(); js("window.onAtlasSearchDone && window.onAtlasSearchDone()") }, 15000)
    }

    @Suppress("DEPRECATION")
    private fun resolveNext() {
        val mgr = nsd ?: return
        if (resolveBusy) return
        val info = resolving.removeFirstOrNull() ?: return
        resolveBusy = true
        try {
            mgr.resolveService(info, object : NsdManager.ResolveListener {
                override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                    main.post { resolveBusy = false; resolveNext() }
                }

                override fun onServiceResolved(serviceInfo: NsdServiceInfo) {
                    main.post {
                        resolveBusy = false
                        val host = serviceInfo.host?.hostAddress
                        if (host != null && !host.contains(':')) {
                            val url = "http://$host:${serviceInfo.port}"
                            val o = JSONObject().put("name", serviceInfo.serviceName).put("url", url)
                            found[url] = o
                            js("window.onAtlasFound && window.onAtlasFound(${JSONArray(found.values.toList())})")
                        }
                        resolveNext()
                    }
                }
            })
        } catch (e: Exception) {
            resolveBusy = false
        }
    }

    private fun stopDiscovery() {
        val mgr = nsd
        val l = discovery
        discovery = null
        if (mgr != null && l != null) try { mgr.stopServiceDiscovery(l) } catch (e: Exception) {}
        resolving.clear()
        resolveBusy = false
        try { multicast?.release() } catch (e: Exception) {}
        multicast = null
    }

    // ------------------------------------------------------------------ checking an address
    private fun connect(address: String, code: String) {
        val base = normalize(address)
        if (base == null) { js("window.onAtlasError && window.onAtlasError(${JSONObject.quote("That doesn't look like an address")})"); return }
        Thread {
            var err: String? = null
            try {
                val c = URL("$base/r/api/tv/hello").openConnection() as HttpURLConnection
                c.connectTimeout = 5000
                c.readTimeout = 5000
                c.setRequestProperty("X-Marquee", "1")
                val body = c.inputStream.bufferedReader().use { it.readText() }
                if (!JSONObject(body).optBoolean("atlas")) err = "Something answered at $base, but it isn't Atlas"
            } catch (e: Exception) {
                err = "Can't reach Atlas at $base. Check the address, and that Atlas Mobile is on (Settings › Phone & casting)."
            }
            main.post {
                if (err != null) {
                    js("window.onAtlasError && window.onAtlasError(${JSONObject.quote(err)})")
                } else {
                    stopDiscovery()
                    prefs.edit().putString("url", base).apply()
                    val clean = code.uppercase().filter { it.isLetterOrDigit() }
                    web.loadUrl("$base/r/tv" + if (clean.length == 6) "#code=$clean" else "")
                }
            }
        }.start()
    }

    /** "192.168.1.20", "pc.tailnet.ts.net:8737", "http://100.64.0.1:8737/r" -> "http://host:port" */
    private fun normalize(address: String): String? {
        var a = address.trim()
        if (a.isEmpty()) return null
        if (!a.contains("://")) a = "http://$a"
        return try {
            val u = URI(a)
            val host = u.host ?: return null
            val scheme = if (u.scheme == "https") "https" else "http"
            val port = if (u.port > 0) u.port else if (scheme == "https") 443 else 8737
            val h = if (host.contains(':') && !host.startsWith("[")) "[$host]" else host
            if ((scheme == "https" && port == 443)) "$scheme://$h" else "$scheme://$h:$port"
        } catch (e: Exception) {
            null
        }
    }

    private fun js(code: String) = main.post { web.evaluateJavascript(code, null) }

    // ------------------------------------------------------------------ what the pages can ask for
    inner class Bridge {
        @JavascriptInterface
        fun keepAwake(on: Boolean) = main.post {
            if (on) window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }

        /** "Change PC" in the TV page's settings. */
        @JavascriptInterface
        fun setup() = main.post {
            val q = StringBuilder("file:///android_asset/setup.html?change=1")
            savedUrl()?.let { q.append("&addr=").append(URLEncoder.encode(it, "UTF-8")) }
            web.loadUrl(q.toString())
        }

        @JavascriptInterface
        fun saved(): String = savedUrl() ?: ""

        @JavascriptInterface
        fun discover() = main.post { if (onSetupPage()) startDiscovery() }

        @JavascriptInterface
        fun connect(address: String, code: String) = main.post { if (onSetupPage()) this@MainActivity.connect(address, code) }

        @JavascriptInterface
        fun version(): String = BuildConfigVersion.name(this@MainActivity)
    }
}

/** The app's version name without BuildConfig (kept off to stay small). */
object BuildConfigVersion {
    fun name(ctx: Context): String = try {
        @Suppress("DEPRECATION")
        ctx.packageManager.getPackageInfo(ctx.packageName, 0).versionName ?: "1"
    } catch (e: Exception) {
        "1"
    }
}
