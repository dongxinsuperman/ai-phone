package org.personal.aiphone.lifecycle;

import android.app.Activity;
import android.content.pm.PackageInfo;
import android.net.Uri;
import android.os.Bundle;
import android.os.SystemClock;
import android.system.Os;
import android.system.OsConstants;
import android.util.Base64;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.ValueCallback;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileDescriptor;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.concurrent.atomic.AtomicInteger;
import org.json.JSONObject;
import org.json.JSONTokener;

public final class MainActivity extends Activity {
  private static final String STORAGE_PROBE = /* A3_READONLY_STORAGE_JS */ "";
  private static final String ORIGIN = "http://127.0.0.1:19080";
  private final AtomicInteger sequence = new AtomicInteger();
  private String run;
  private String phase;
  private WebView view;

  @Override public void onCreate(Bundle state) {
    super.onCreate(state);
    String url = getIntent().getStringExtra("url");
    if (url == null || !url.startsWith(ORIGIN + "/fixture/")) {
      throw new IllegalArgumentException("personal loopback fixture URL required");
    }
    Uri location = Uri.parse(url);
    run = location.getQueryParameter("run");
    phase = getIntent().getStringExtra("witness_mode");
    if (run == null || !run.matches("android-[A-Za-z0-9_-]{8,70}") ||
        !("first-write".equals(phase) || "restart-read".equals(phase))) {
      throw new IllegalArgumentException("exact personal diagnostic run/mode required");
    }

    // The only write happens before the first WebView exists. Restart takes a
    // separate read-only branch and never recreates a missing witness.
    JSONObject witness = witness();
    emit("witness", witness);
    if ("first-write".equals(phase) && !witness.optBoolean("ok", false)) {
      // Explicit failed precondition; do not run the page after a failed fsync.
      return;
    }

    view = new WebView(this);
    view.getSettings().setJavaScriptEnabled(true);
    view.getSettings().setDomStorageEnabled(true);
    view.setWebViewClient(new WebViewClient() {
      @Override public WebResourceResponse shouldInterceptRequest(WebView current, WebResourceRequest request) {
        Uri uri = request.getUrl();
        if ((ORIGIN + "/fixture/receipt").equals(uri.toString())) {
          final long observedWall = System.currentTimeMillis();
          final long observedElapsed = SystemClock.elapsedRealtimeNanos();
          // Observing the original POST does not change it or add a page
          // listener. Evaluation and diagnostic delivery are asynchronous.
          current.post(new Runnable() {
            @Override public void run() { readStorage("receipt-request", observedWall, observedElapsed); }
          });
        }
        return super.shouldInterceptRequest(current, request);
      }
      @Override public void onPageFinished(WebView current, String finishedUrl) {
        super.onPageFinished(current, finishedUrl);
        readStorage("page-finished", System.currentTimeMillis(), SystemClock.elapsedRealtimeNanos());
      }
    });
    setContentView(view);
    JSONObject provider = new JSONObject();
    try {
      PackageInfo info = WebView.getCurrentWebViewPackage();
      if (info == null) throw new IllegalStateException("actual WebView provider unavailable");
      provider.put("packageName", info.packageName);
      provider.put("versionName", info.versionName);
      provider.put("longVersionCode", info.getLongVersionCode());
      provider.put("ok", true);
    } catch (Exception error) {
      putError(provider, error);
    }
    emit("webview-provider", provider);
    android.util.Log.i("PersonalVmE2E", "fixture-url=" + url);
    view.loadUrl(url);
  }

  private JSONObject witness() {
    JSONObject result = new JSONObject();
    try {
      result.put("startedWallMs", System.currentTimeMillis());
      result.put("startedElapsedNs", SystemClock.elapsedRealtimeNanos());
      result.put("mode", phase);
      result.put("openedForWrite", false);
      result.put("created", false);
      result.put("fileFsync", false);
      result.put("parentFsync", false);
      String name = getIntent().getStringExtra("witness_name");
      if (!(("personal-witness-" + run + ".txt").equals(name))) {
        throw new IllegalArgumentException("unexpected witness filename");
      }
      // getDataDir is an existing installation directory; unlike getFilesDir,
      // this read branch does not create a missing directory as a side effect.
      File parent = getDataDir().getCanonicalFile();
      if (!parent.isDirectory()) throw new IllegalStateException("app data directory is absent");
      File file = new File(parent, name).getCanonicalFile();
      if (!parent.equals(file.getParentFile())) throw new IllegalArgumentException("witness escaped app data directory");
      result.put("path", file.getPath());
      result.put("parent", parent.getPath());
      result.put("existedBefore", file.exists());

      if ("first-write".equals(phase)) {
        String encoded = getIntent().getStringExtra("witness_payload_b64");
        if (encoded == null) throw new IllegalArgumentException("first-write payload is absent");
        byte[] payload = Base64.decode(encoded, Base64.NO_WRAP);
        if (payload.length == 0 || payload.length > 8192) throw new IllegalArgumentException("witness payload size invalid");
        if (!file.createNewFile()) throw new IllegalStateException("witness already exists; never truncate or repair");
        result.put("created", true);
        result.put("openedForWrite", true);
        try (FileOutputStream output = new FileOutputStream(file, false)) {
          output.write(payload);
          output.getFD().sync();
          result.put("fileFsync", true);
        }
        FileDescriptor parentFd = Os.open(parent.getPath(), OsConstants.O_RDONLY, 0);
        try {
          if (!OsConstants.S_ISDIR(Os.fstat(parentFd).st_mode)) throw new IllegalStateException("witness parent FD is not a directory");
          Os.fsync(parentFd);
          result.put("parentFsync", true);
        } finally {
          Os.close(parentFd);
        }
      } else if ("restart-read".equals(phase)) {
        // No FileOutputStream, createNewFile, mkdir, retry, or write in this branch.
        if (!file.isFile()) throw new IllegalStateException("witness missing after restart; not repaired");
      } else {
        throw new IllegalArgumentException("unknown witness mode");
      }

      if (!file.isFile() || file.length() > 8192) throw new IllegalStateException("witness file is invalid");
      ByteArrayOutputStream bytes = new ByteArrayOutputStream();
      try (FileInputStream input = new FileInputStream(file)) {
        byte[] chunk = new byte[1024];
        int count;
        while ((count = input.read(chunk)) != -1) bytes.write(chunk, 0, count);
      }
      byte[] payload = bytes.toByteArray();
      byte[] digest = MessageDigest.getInstance("SHA-256").digest(payload);
      StringBuilder sha = new StringBuilder();
      for (byte value : digest) sha.append(String.format(java.util.Locale.ROOT, "%02x", value & 255));
      result.put("sha256", sha.toString());
      result.put("bytes", payload.length);
      result.put("inode", Os.stat(file.getPath()).st_ino);
      result.put("mtimeMs", file.lastModified());
      result.put("ok", true);
    } catch (Exception error) {
      putError(result, error);
    }
    try {
      result.put("finishedWallMs", System.currentTimeMillis());
      result.put("finishedElapsedNs", SystemClock.elapsedRealtimeNanos());
    } catch (Exception impossible) { throw new IllegalStateException(impossible); }
    return result;
  }

  private void readStorage(String trigger, long observedWall, long observedElapsed) {
    final long requestedWall = System.currentTimeMillis();
    final long requestedElapsed = SystemClock.elapsedRealtimeNanos();
    view.evaluateJavascript(STORAGE_PROBE, new ValueCallback<String>() {
      @Override public void onReceiveValue(String value) {
      JSONObject result = new JSONObject();
      try {
        Object decoded = new JSONTokener(value).nextValue();
        if (!(decoded instanceof String)) throw new IllegalStateException("storage probe did not return JSON text");
        result = new JSONObject((String) decoded);
        result.put("probeOk", true);
      } catch (Exception error) {
        putError(result, error);
        try { result.put("probeOk", false); } catch (Exception impossible) { throw new IllegalStateException(impossible); }
      }
      try {
        result.put("trigger", trigger);
        result.put("triggerWallMs", observedWall);
        result.put("triggerElapsedNs", observedElapsed);
        result.put("evaluationRequestedWallMs", requestedWall);
        result.put("evaluationRequestedElapsedNs", requestedElapsed);
        result.put("evaluationFinishedWallMs", System.currentTimeMillis());
        result.put("evaluationFinishedElapsedNs", SystemClock.elapsedRealtimeNanos());
      } catch (Exception impossible) { throw new IllegalStateException(impossible); }
      emit("storage", result);
      }
    });
  }

  private static void putError(JSONObject value, Exception error) {
    try {
      value.put("ok", false);
      value.put("errorClass", error.getClass().getName());
      value.put("error", String.valueOf(error.getMessage()));
    } catch (Exception impossible) { throw new IllegalStateException(impossible); }
  }

  private void emit(String kind, JSONObject data) {
    final String encoded;
    final int index = sequence.incrementAndGet();
    try {
      JSONObject body = new JSONObject();
      body.put("protocol", "personal-vm-a3");
      body.put("run", run);
      body.put("platform", "android");
      body.put("phase", phase);
      body.put("kind", kind);
      body.put("sequence", index);
      body.put("emittedWallMs", System.currentTimeMillis());
      body.put("emittedElapsedNs", SystemClock.elapsedRealtimeNanos());
      body.put("data", data);
      encoded = body.toString();
    } catch (Exception error) { throw new IllegalStateException(error); }
    android.util.Log.i("PersonalVmA3", encoded);
    new Thread(new Runnable() {
      @Override public void run() {
      HttpURLConnection connection = null;
      try {
        connection = (HttpURLConnection) new URL(ORIGIN + "/fixture/diagnostic").openConnection();
        connection.setConnectTimeout(3000);
        connection.setReadTimeout(3000);
        connection.setRequestMethod("POST");
        connection.setDoOutput(true);
        connection.setRequestProperty("Content-Type", "application/json");
        try (OutputStream output = connection.getOutputStream()) {
          output.write(encoded.getBytes(StandardCharsets.UTF_8));
        }
        int status = connection.getResponseCode();
        android.util.Log.i("PersonalVmA3Delivery", "sequence=" + index + " status=" + status);
      } catch (Exception error) {
        // Evidence delivery is not retried and never stalls the original page.
        android.util.Log.e("PersonalVmA3Delivery", "sequence=" + index + " error=" + error, error);
      } finally {
        if (connection != null) connection.disconnect();
      }
      }
    }, "personal-vm-a3-evidence-" + index).start();
  }
}
