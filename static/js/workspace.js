(() => {
  const AppConfig = window.APP;
  const getElement = (selector) => document.querySelector(selector);
  const getElements = (selector) => [...document.querySelectorAll(selector)];

  const escapeHtml = (text) =>
    String(text ?? '').replace(
      /[&<>"']/g,
      (char) =>
        ({
          '&': '&amp;',
          '<': '&lt;',
          '>': '&gt;',
          '"': '&quot;',
          "'": '&#39;',
        }[char])
    );

  const getIconSvg = (name, className = '') =>
    `<svg class="ic ${className}"><use href="#i-${name}"/></svg>`;

  const getCsrfToken = () =>
    (document.cookie.split('; ').find((row) => row.startsWith('csrftoken=')) || '').split('=')[1] || '';

  const AppState = {
    permissions: AppConfig.perms,
    currentUser: AppConfig.me,
    models: [],
    members: [],
    invites: [],
    onlineUsers: [],
    settings: {
      threshold: 0.7,
      unique: true,
      numbering: false,
      model_id: null,
      running: true,
      brightness: 100,
      limits: {},
    },
    predictions: [],
    objectCounts: {},
    uniqueCounts: {},
    strokes: [],
    showDescription: false,
    drawMode: false,
    aspectRatio: 4 / 3,
    mirrorCamera: true,
    isPaused: false,
    isBlankStream: false,
    lastPacketTimestamp: 0,
    countsSignature: '',
    currentFps: 0,
    systemCapabilities: { max_ram_mb: 100, cpu_count: 1 },
  };

  const FRAME_INTERVAL_MS = 40;
  const MAX_FRAME_DIMENSION = 480;

  let processedFramesCount = 0;
  let isStreamSocketReady = false;
  let isTelemetrySocketReady = false;
  let isAccessClosedHandled = false;

  function showToast(message) {
    const toastElement = document.createElement('div');
    toastElement.className = 'toast';
    toastElement.textContent = message;
    document.body.append(toastElement);
    setTimeout(() => toastElement.remove(), 3600);
  }

  async function apiRequest(path, options = {}) {
    const requestUrl = `/p/${AppConfig.pid}/api/${path}`;
    const httpMethod = options.method || (options.json || options.body ? 'POST' : 'GET');
    const headers = {
      'X-CSRFToken': getCsrfToken(),
      ...(options.json ? { 'Content-Type': 'application/json' } : {}),
    };

    const response = await fetch(requestUrl, {
      method: httpMethod,
      credentials: 'same-origin',
      headers: headers,
      body: options.json ? JSON.stringify(options.json) : options.body,
    });

    let responseData = {};
    try {
      responseData = await response.json();
    } catch {}

    if (!response.ok || responseData.ok === false) {
      throw new Error(responseData.error || response.statusText);
    }
    return responseData;
  }

  const buildWebSocketUrl = (path) =>
    `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${path}`;

  function handleAccessKicked() {
    if (isAccessClosedHandled) return;
    isAccessClosedHandled = true;
    showToast('Project access was closed');
    setTimeout(() => (location.href = '/projects/'), 1500);
  }

  function createWebSocketConnection(path, onMessageCallback, onStateChangeCallback) {
    let socket = null;
    let reconnectDelayMs = 500;

    const connect = () => {
      socket = new WebSocket(buildWebSocketUrl(path));
      socket.binaryType = 'arraybuffer';

      socket.onopen = () => {
        reconnectDelayMs = 500;
        onStateChangeCallback(true);
      };

      socket.onmessage = onMessageCallback;

      socket.onclose = (event) => {
        onStateChangeCallback(false);
        if (event.code === 4403) {
          return handleAccessKicked();
        }
        reconnectDelayMs = Math.min(reconnectDelayMs * 2, 8000);
        setTimeout(connect, reconnectDelayMs);
      };
    };

    connect();

    return {
      send: (data) => socket && socket.readyState === WebSocket.OPEN && socket.send(data),
      get ready() {
        return Boolean(socket && socket.readyState === WebSocket.OPEN);
      },
    };
  }

  function updateConnectionStatus() {
    const isOnline = isStreamSocketReady && isTelemetrySocketReady;
    const sourceKindNames = {
      camera: 'PC camera',
      video: 'video file',
      image: 'photo',
    };
    const currentKindName = sourceKindNames[VideoSource.kind];

    const statusElement = getElement('#status');
    const statusTextElement = getElement('#status-t');

    statusElement.dataset.s = isOnline ? 'ok' : 'err';
    statusTextElement.textContent = isOnline
      ? 'Online' + (currentKindName ? ' · ' + currentKindName : '')
      : 'No connection';
  }

  getElements('.nav button').forEach((button) => {
    button.onclick = () => {
      getElements('.nav button').forEach((navBtn) =>
        navBtn.setAttribute('aria-selected', navBtn === button)
      );
      getElements('.panel').forEach(
        (panel) => (panel.hidden = panel.id !== 'p-' + button.dataset.tab)
      );
    };
  });

  getElement('#burger').onclick = () => getElement('#app').classList.toggle('open');
  getElement('#scrim').onclick = () => getElement('#app').classList.remove('open');

  function applyPermissions() {
    getElements('[data-need]').forEach((element) => {
      const hasPermission = Boolean(AppState.permissions[element.dataset.need]);
      if (['BUTTON', 'INPUT', 'SELECT', 'TEXTAREA'].includes(element.tagName)) {
        element.disabled = !hasPermission;
        if (!hasPermission) {
          element.title = element.title || 'No permission';
        }
      } else {
        element.style.display = hasPermission ? '' : 'none';
      }
    });

    getElements('[data-nowner]').forEach((element) => {
      element.style.display = AppState.permissions.owner ? 'none' : '';
    });

    getElement('#range-br').disabled = !(
      AppState.permissions.stream || AppState.permissions.control
    );
    updateControlButtons();
  }

  class SparklineChart {
    constructor(canvasSelector, strokeColor, fixedMax) {
      this.canvas = getElement(canvasSelector);
      this.strokeColor = strokeColor;
      this.fixedMax = fixedMax;
      this.values = Array(90).fill(0);
    }

    push(value) {
      this.values.push(value);
      this.values.shift();
      this.draw();
    }

    draw() {
      const canvas = this.canvas;
      const devicePixelRatio = window.devicePixelRatio || 1;
      const width = canvas.clientWidth * devicePixelRatio;
      const height = canvas.clientHeight * devicePixelRatio;

      if (!width || !height) return;

      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }

      const context = canvas.getContext('2d');
      context.clearRect(0, 0, width, height);

      const maxValue = this.fixedMax || Math.max(1, ...this.values) * 1.15;
      context.beginPath();

      this.values.forEach((val, index) => {
        const pointX = (index / (this.values.length - 1)) * width;
        const normalizedY = Math.min(val / maxValue, 1);
        const pointY = height - normalizedY * (height - 4 * devicePixelRatio) - 2 * devicePixelRatio;

        if (index === 0) {
          context.moveTo(pointX, pointY);
        } else {
          context.lineTo(pointX, pointY);
        }
      });

      context.strokeStyle = this.strokeColor;
      context.lineWidth = 2 * devicePixelRatio;
      context.stroke();

      context.lineTo(width, height);
      context.lineTo(0, height);
      context.fillStyle = this.strokeColor + '22';
      context.fill();
    }
  }

  const charts = {
    cpu: new SparklineChart('#c-cpu', '#0f766e', 100),
    ram: new SparklineChart('#c-ram', '#3b6cf6'),
    fps: new SparklineChart('#c-fps', '#ff5a1f'),
    latency: new SparklineChart('#c-lat', '#c2379b'),
  };

  function onTelemetryData(message) {
    const systemMetrics = message.system;
    const workerMetrics = systemMetrics.worker;
    const limits = AppState.settings.limits || {};

    charts.cpu.push(workerMetrics ? workerMetrics.cpu : 0);
    charts.ram.fixedMax = limits.ram_mb || 100;
    charts.ram.push(workerMetrics ? workerMetrics.rss_mb : 0);
    charts.fps.push(systemMetrics.fps);
    charts.latency.push(systemMetrics.latency_ms);

    getElement('#v-cpu').textContent = workerMetrics ? `${workerMetrics.cpu}%` : '—';
    getElement('#v-ram').textContent = workerMetrics ? `${workerMetrics.rss_mb}/${limits.ram_mb} MB` : '—';
    getElement('#v-fps').textContent = systemMetrics.fps;
    getElement('#v-lat').textContent = `${systemMetrics.latency_ms} ms`;
    getElement('#v-cpu').title = `System: CPU ${systemMetrics.cpu}%, RAM ${systemMetrics.ram_percent}%`;

    getElement('#res-status').textContent = workerMetrics
      ? `${workerMetrics.engine || 'engine'} · peak ${workerMetrics.peak_mb} MB · ${workerMetrics.enforcement.join(' · ')}`
      : AppState.settings.model_id && AppState.settings.running === false
      ? 'Model stopped — worker shut down, RAM freed'
      : 'Model not loaded';

    AppState.currentFps = systemMetrics.fps;

    const consoleContainer = getElement('#console');
    const isScrolledToBottom =
      consoleContainer.scrollTop + consoleContainer.clientHeight > consoleContainer.scrollHeight - 30;

    for (const logItem of message.logs) {
      const logLine = document.createElement('div');
      logLine.className = logItem.level;
      const timeFormatted = new Date(logItem.ts * 1000).toLocaleTimeString('en-GB');
      logLine.textContent = `${timeFormatted} [${logItem.level}] ${logItem.msg}`;
      consoleContainer.append(logLine);
    }

    while (consoleContainer.children.length > 300) {
      consoleContainer.firstChild.remove();
    }

    if (isScrolledToBottom) {
      consoleContainer.scrollTop = consoleContainer.scrollHeight;
    }
  }

  getElement('#btn-conclear').onclick = () => getElement('#console').replaceChildren();

  const stageArea = getElement('#stagearea');
  const videoFrame = getElement('#frame');
  const videoViewCanvas = getElement('#view');
  const overlayCanvas = getElement('#overlay');
  const drawingCanvas = getElement('#draw');

  function fitStageLayout() {
    const areaWidth = stageArea.clientWidth;
    const areaHeight = stageArea.clientHeight;

    let frameWidth = areaWidth;
    let frameHeight = areaWidth / AppState.aspectRatio;

    if (frameHeight > areaHeight) {
      frameHeight = areaHeight;
      frameWidth = areaHeight * AppState.aspectRatio;
    }

    videoFrame.style.width = `${frameWidth}px`;
    videoFrame.style.height = `${frameHeight}px`;

    const devicePixelRatio = window.devicePixelRatio || 1;
    for (const canvas of [overlayCanvas, drawingCanvas]) {
      canvas.width = Math.round(frameWidth * devicePixelRatio);
      canvas.height = Math.round(frameHeight * devicePixelRatio);
    }

    drawOverlay();
    drawStrokes();
  }

  new ResizeObserver(fitStageLayout).observe(stageArea);

  getElement('#btn-fs').onclick = () => {
    if (stageArea.requestFullscreen) {
      if (document.fullscreenElement) {
        document.exitFullscreen();
      } else {
        stageArea.requestFullscreen();
      }
    } else {
      stageArea.classList.toggle('pfs');
      fitStageLayout();
    }
  };

  document.addEventListener('fullscreenchange', () => {
    getElement('#ic-fs use').setAttribute(
      'href',
      document.fullscreenElement ? '#i-min' : '#i-max'
    );
    setTimeout(fitStageLayout, 50);
  });

  function drawOverlay() {
    const context = overlayCanvas.getContext('2d');
    const width = overlayCanvas.width;
    const height = overlayCanvas.height;
    const devicePixelRatio = window.devicePixelRatio || 1;

    context.clearRect(0, 0, width, height);

    for (const prediction of AppState.predictions) {
      const centerX = prediction.x * width;
      const centerY = prediction.y * height;
      const boxWidth = prediction.w * width;
      const boxHeight = prediction.h * height;

      context.strokeStyle = prediction.color + 'bb';
      context.lineWidth = 1.5 * devicePixelRatio;
      context.strokeRect(centerX - boxWidth / 2, centerY - boxHeight / 2, boxWidth, boxHeight);

      const radius = AppState.settings.unique
        ? Math.max(6, Math.min(16, Math.min(boxWidth, boxHeight) * 0.3)) * devicePixelRatio
        : 6 * devicePixelRatio;

      context.beginPath();
      context.arc(centerX, centerY, radius, 0, 2 * Math.PI);
      context.fillStyle = prediction.color;
      context.fill();
      context.lineWidth = 2 * devicePixelRatio;
      context.strokeStyle = '#fff';
      context.stroke();

      const trackPrefix =
        AppState.settings.numbering && prediction.track_id ? `#${prediction.track_id} ` : '';
      const confidencePercent = Math.round(prediction.confidence * 100);
      const titleLine = `${trackPrefix}${prediction.label} ${confidencePercent}%`;

      const lines = [titleLine];
      if (AppState.showDescription && prediction.description) {
        lines.push(prediction.description);
      }

      context.font = `600 ${12 * devicePixelRatio}px Onest, sans-serif`;
      const maxTextWidth = Math.max(...lines.map((l) => context.measureText(l).width));
      const boxTextWidth = maxTextWidth + 10 * devicePixelRatio;
      const boxTextHeight = lines.length * 16 * devicePixelRatio + 4 * devicePixelRatio;

      let textX = centerX + radius + 6 * devicePixelRatio;
      let textY = centerY - boxTextHeight / 2;

      if (textX + boxTextWidth > width) {
        textX = centerX - radius - 6 * devicePixelRatio - boxTextWidth;
      }
      textY = Math.max(2, Math.min(height - boxTextHeight - 2, textY));

      context.fillStyle = '#0b0f13cc';
      context.fillRect(textX, textY, boxTextWidth, boxTextHeight);

      lines.forEach((lineText, index) => {
        context.fillStyle = index ? '#c7d0d6' : prediction.color;
        context.font = `${index ? 400 : 600} ${12 * devicePixelRatio}px Onest, sans-serif`;
        context.fillText(
          lineText,
          textX + 5 * devicePixelRatio,
          textY + (14 + index * 16) * devicePixelRatio
        );
      });
    }
  }

  function paintStroke(context, stroke, width, height) {
    if (!stroke.pts.length) return;

    context.beginPath();
    context.lineCap = context.lineJoin = 'round';
    context.strokeStyle = stroke.c;
    context.lineWidth = Math.max(1, stroke.w * width);

    stroke.pts.forEach(([pointX, pointY], index) => {
      if (index === 0) {
        context.moveTo(pointX * width, pointY * height);
      } else {
        context.lineTo(pointX * width, pointY * height);
      }
    });

    if (stroke.pts.length === 1) {
      context.lineTo(stroke.pts[0][0] * width + 0.1, stroke.pts[0][1] * height);
    }
    context.stroke();
  }

  function drawStrokes() {
    const context = drawingCanvas.getContext('2d');
    context.clearRect(0, 0, drawingCanvas.width, drawingCanvas.height);
    AppState.strokes.forEach((stroke) => paintStroke(context, stroke, drawingCanvas.width, drawingCanvas.height));
  }

  let activeStroke = null;

  const getNormalizedPoint = (event) => {
    const rect = drawingCanvas.getBoundingClientRect();
    return [(event.clientX - rect.left) / rect.width, (event.clientY - rect.top) / rect.height];
  };

  drawingCanvas.onpointerdown = (event) => {
    if (!AppState.drawMode) return;
    drawingCanvas.setPointerCapture(event.pointerId);
    activeStroke = {
      c: getElement('#dr-color').value,
      w: +getElement('#dr-size').value / 800,
      pts: [getNormalizedPoint(event)],
    };
  };

  drawingCanvas.onpointermove = (event) => {
    if (!activeStroke) return;
    activeStroke.pts.push(getNormalizedPoint(event));
    drawStrokes();
    paintStroke(drawingCanvas.getContext('2d'), activeStroke, drawingCanvas.width, drawingCanvas.height);
  };

  drawingCanvas.onpointerup = () => {
    if (!activeStroke) return;
    AppState.strokes.push(activeStroke);
    telemetrySocket.send(JSON.stringify({ type: 'draw', stroke: activeStroke }));
    activeStroke = null;
    drawStrokes();
  };

  getElement('#btn-draw').onclick = (event) => {
    AppState.drawMode = !AppState.drawMode;
    event.currentTarget.classList.toggle('on', AppState.drawMode);
    getElement('#dopts').hidden = !AppState.drawMode;
    drawingCanvas.style.pointerEvents = AppState.drawMode ? 'auto' : 'none';
    drawingCanvas.style.cursor = 'crosshair';
  };

  getElement('#btn-clear').onclick = () =>
    telemetrySocket.send(JSON.stringify({ type: 'clear_draw' }));

  drawingCanvas.style.pointerEvents = 'none';

  function setPlaceholderState() {
    const placeholderElement = getElement('#ph');
    placeholderElement.hidden = !videoFrame.hidden;

    if (!videoFrame.hidden) return;

    if (AppState.isBlankStream) {
      placeholderElement.innerHTML = `${getIconSvg('eyeoff', 'big')}<b>Stream hidden</b><span>The screen was cleared. Start your own source or show the stream again.</span><button class="btn sm" id="btn-unhide">${getIconSvg('eye')}Show stream</button>`;
    } else if (!AppState.settings.model_id) {
      placeholderElement.innerHTML = `${getIconSvg('box', 'big')}<b>Choose a model first</b><span>Open the “Model” tab on the left and pick or upload a .tflite.</span>`;
    } else {
      placeholderElement.innerHTML = `${getIconSvg('camera', 'big')}<b>No video</b><span>Turn on the camera, load a file or connect a phone via QR.</span>`;
    }

    const unhideBtn = getElement('#btn-unhide');
    if (unhideBtn) {
      unhideBtn.onclick = () => {
        AppState.isBlankStream = false;
        setPlaceholderState();
      };
    }
  }

  async function showPacket(buffer) {
    AppState.lastPacketTimestamp = Date.now();
    if (AppState.isPaused || AppState.isBlankStream) return;

    const dataView = new DataView(buffer);
    const headerLength = dataView.getUint32(0);
    const headerJsonText = new TextDecoder().decode(
      new Uint8Array(buffer, 4, headerLength)
    );
    const headerData = JSON.parse(headerJsonText);

    const currentSequence = ++processedFramesCount;
    const imageBitmap = await createImageBitmap(
      new Blob([new Uint8Array(buffer, 4 + headerLength)], { type: 'image/jpeg' })
    );

    if (currentSequence !== processedFramesCount || AppState.isPaused || AppState.isBlankStream) {
      imageBitmap.close();
      return;
    }

    if (videoViewCanvas.width !== imageBitmap.width || videoViewCanvas.height !== imageBitmap.height) {
      videoViewCanvas.width = imageBitmap.width;
      videoViewCanvas.height = imageBitmap.height;
      AppState.aspectRatio = imageBitmap.width / imageBitmap.height;
      fitStageLayout();
    }

    videoViewCanvas.getContext('2d').drawImage(imageBitmap, 0, 0);
    imageBitmap.close();

    videoViewCanvas.style.filter = `brightness(${headerData.brightness || 100}%)`;

    if (videoFrame.hidden) {
      videoFrame.hidden = false;
      setPlaceholderState();
      updateControlButtons();
    }

    const countsSignature =
      JSON.stringify(headerData.counts) + JSON.stringify(headerData.unique_total);

    AppState.predictions = headerData.predictions || [];
    drawOverlay();

    if (countsSignature !== AppState.countsSignature) {
      AppState.countsSignature = countsSignature;
      AppState.objectCounts = headerData.counts || {};
      AppState.uniqueCounts = headerData.unique_total || {};
      renderDetectedObjects();
    }

    const hudElement = getElement('#hud');
    hudElement.textContent = headerData.error
      ? '⚠ ' + headerData.error
      : headerData.stopped
      ? '⏸ Model stopped — video without detection'
      : `${AppState.currentFps || 0} FPS · inference ${headerData.inference_ms} ms · from ${headerData.source}`;

    hudElement.hidden = !(
      headerData.error ||
      headerData.stopped ||
      getElement('#sw-perf').checked
    );
  }

  const mediaSourceElement = getElement('#src');
  const offscreenCanvas = document.createElement('canvas');

  const VideoSource = {
    kind: null,
    stream: null,
    element: null,
    timer: null,
    isFrameInFlight: false,
    lastSentAt: 0,
    objectUrl: null,
  };

  function stopSource() {
    clearTimeout(VideoSource.timer);
    VideoSource.kind = null;
    VideoSource.element = null;

    if (VideoSource.stream) {
      VideoSource.stream.getTracks().forEach((track) => track.stop());
      VideoSource.stream = null;
    }

    mediaSourceElement.pause();
    mediaSourceElement.srcObject = null;
    mediaSourceElement.removeAttribute('src');
    try {
      mediaSourceElement.load();
    } catch {}

    if (VideoSource.objectUrl) {
      URL.revokeObjectURL(VideoSource.objectUrl);
      VideoSource.objectUrl = null;
    }

    updateConnectionStatus();
    updateControlButtons();
  }

  try {
    const savedCamConfig = JSON.parse(localStorage.getItem('fomo_cam') || '{}');
    if ('mirror' in savedCamConfig) {
      AppState.mirrorCamera = Boolean(savedCamConfig.mirror);
    }
  } catch {}

  function updateCameraFlags() {
    getElement('#btn-mirror').classList.toggle('on', AppState.mirrorCamera);
    try {
      localStorage.setItem('fomo_cam', JSON.stringify({ mirror: AppState.mirrorCamera }));
    } catch {}
  }

  getElement('#btn-mirror').onclick = () => {
    AppState.mirrorCamera = !AppState.mirrorCamera;
    updateCameraFlags();
  };
  updateCameraFlags();

  function beginSource(kind, element) {
    VideoSource.kind = kind;
    VideoSource.element = element;
    AppState.isPaused = false;
    AppState.isBlankStream = false;
    updateConnectionStatus();
    setPlaceholderState();
    updateControlButtons();
  }

  getElement('#btn-cam').onclick = async () => {
    if (!navigator.mediaDevices?.getUserMedia) {
      return showToast('Camera unavailable: HTTPS or localhost is required');
    }

    stopSource();

    try {
      VideoSource.stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 640 }, height: { ideal: 480 } },
        audio: false,
      });
    } catch (error) {
      return showToast('Could not open the camera: ' + error.message);
    }

    mediaSourceElement.loop = false;
    mediaSourceElement.srcObject = VideoSource.stream;
    await mediaSourceElement.play();
    beginSource('camera', mediaSourceElement);
    startSourceLoop();
  };

  getElement('#btn-file').onclick = () => getElement('#file').click();

  getElement('#file').onchange = (event) => {
    const file = event.target.files[0];
    if (!file) return;

    stopSource();
    VideoSource.objectUrl = URL.createObjectURL(file);

    if (file.type.startsWith('image/')) {
      const img = new Image();
      img.onload = () => {
        beginSource('image', img);
        sendFrame();
      };
      img.src = VideoSource.objectUrl;
    } else {
      mediaSourceElement.loop = true;
      mediaSourceElement.src = VideoSource.objectUrl;
      mediaSourceElement.onloadeddata = () => {
        mediaSourceElement.play();
        beginSource('video', mediaSourceElement);
        startSourceLoop();
      };
    }
    event.target.value = '';
  };

  function startSourceLoop() {
    if (!VideoSource.kind || VideoSource.kind === 'image') return;

    if (
      !AppState.isPaused &&
      (!VideoSource.isFrameInFlight || Date.now() - VideoSource.lastSentAt > 3000) &&
      isStreamSocketReady
    ) {
      sendFrame();
    }

    VideoSource.timer = setTimeout(
      startSourceLoop,
      AppState.isPaused ? 120 : FRAME_INTERVAL_MS
    );
  }

  function sendFrame() {
    const element = VideoSource.element;
    if (!element || !streamSocket.ready) return;

    const sourceWidth = element.videoWidth || element.naturalWidth;
    const sourceHeight = element.videoHeight || element.naturalHeight;
    if (!sourceWidth) return;

    const scale = Math.min(1, MAX_FRAME_DIMENSION / Math.max(sourceWidth, sourceHeight));
    offscreenCanvas.width = Math.round(sourceWidth * scale);
    offscreenCanvas.height = Math.round(sourceHeight * scale);

    const context = offscreenCanvas.getContext('2d');
    context.save();

    if (VideoSource.kind === 'camera' && AppState.mirrorCamera) {
      context.translate(offscreenCanvas.width, 0);
      context.scale(-1, 1);
    }

    context.drawImage(element, 0, 0, offscreenCanvas.width, offscreenCanvas.height);
    context.restore();

    VideoSource.isFrameInFlight = true;
    VideoSource.lastSentAt = Date.now();

    offscreenCanvas.toBlob(
      (blob) => {
        if (blob) {
          blob.arrayBuffer().then((buffer) => streamSocket.send(buffer));
        }
      },
      'image/jpeg',
      0.72
    );
  }

  getElement('#btn-pause').onclick = () => {
    AppState.isPaused = !AppState.isPaused;
    if (VideoSource.kind === 'video') {
      if (AppState.isPaused) {
        mediaSourceElement.pause();
      } else {
        mediaSourceElement.play();
      }
    }
    updateControlButtons();
  };

  function resetScreen() {
    const isRemoteStreamLive =
      !VideoSource.kind && Date.now() - AppState.lastPacketTimestamp < 3000;

    stopSource();
    AppState.isPaused = false;
    AppState.isBlankStream = isRemoteStreamLive;
    AppState.predictions = [];
    AppState.objectCounts = {};
    AppState.uniqueCounts = {};
    AppState.countsSignature = '';
    processedFramesCount++;

    videoFrame.hidden = true;
    getElement('#hud').hidden = true;

    drawOverlay();
    renderDetectedObjects();
    setPlaceholderState();
    updateControlButtons();

    if (AppState.permissions.control) {
      telemetrySocket.send(JSON.stringify({ type: 'reset_tracker' }));
    }
  }

  getElement('#btn-reset').onclick = resetScreen;
  getElement('#btn-model').onclick = () =>
    updateSettings({ running: AppState.settings.running === false });

  window.addEventListener('pagehide', stopSource);

  function updateControlButtons() {
    const pauseButton = getElement('#btn-pause');
    pauseButton.disabled = videoFrame.hidden || VideoSource.kind === 'image';
    pauseButton.classList.toggle('on', AppState.isPaused);
    getElement('#ic-pause use').setAttribute(
      'href',
      AppState.isPaused ? '#i-play' : '#i-pause'
    );
    getElement('#pause-lbl').textContent = AppState.isPaused ? 'Resume' : 'Pause';

    const modelId = AppState.settings.model_id;
    const isRunning = AppState.settings.running !== false;
    const modelButton = getElement('#btn-model');

    modelButton.disabled = !(AppState.permissions.control && modelId);
    modelButton.classList.toggle('stopped', Boolean(modelId && !isRunning));
    modelButton.querySelector('use').setAttribute('href', isRunning ? '#i-power' : '#i-play');

    getElement('#model-lbl').textContent = !modelId ? 'Model' : isRunning ? 'Stop' : 'Start';
    modelButton.title = !modelId
      ? 'Choose a model first'
      : isRunning
      ? 'Stop the model (frees memory)'
      : 'Start the model';

    const modelStatusPill = getElement('#mstate');
    modelStatusPill.textContent = !modelId ? 'no model' : isRunning ? 'running' : 'stopped';
    modelStatusPill.className =
      'pill ' + (!modelId ? '' : isRunning ? 'ok' : 'warn');
  }

  const streamSocket = createWebSocketConnection(
    `/ws/stream/${AppConfig.pid}/`,
    (event) => {
      if (typeof event.data === 'string') {
        const message = JSON.parse(event.data);
        if (message.type === 'ack') {
          VideoSource.isFrameInFlight = false;
        }
        return;
      }
      showPacket(event.data);
    },
    (isReady) => {
      isStreamSocketReady = isReady;
      updateConnectionStatus();
    }
  );

  const telemetrySocket = createWebSocketConnection(
    `/ws/telemetry/${AppConfig.pid}/`,
    (event) => {
      const message = JSON.parse(event.data);
      switch (message.type) {
        case 'hello':
          AppState.settings = message.settings;
          AppState.permissions = message.perms;
          AppState.systemCapabilities = message.caps || AppState.systemCapabilities;
          AppState.strokes = message.strokes || [];
          applyPermissions();
          syncBrightnessSlider();
          renderAllPanels();
          drawStrokes();
          break;
        case 'telemetry':
          onTelemetryData(message);
          break;
        case 'settings':
          AppState.settings = message.settings;
          syncBrightnessSlider();
          renderAllPanels();
          if (VideoSource.kind === 'image') sendFrame();
          break;
        case 'presence':
          AppState.onlineUsers = message.online;
          renderMembers();
          break;
        case 'draw':
          AppState.strokes.push(message.stroke);
          drawStrokes();
          break;
        case 'clear_draw':
          AppState.strokes = [];
          drawStrokes();
          break;
        case 'refresh':
          loadProjectState();
          break;
        case 'loading':
        case 'error':
          showToast(message.msg);
          break;
      }
    },
    (isReady) => {
      isTelemetrySocketReady = isReady;
      updateConnectionStatus();
    }
  );

  const updateSettings = (newSettings) =>
    telemetrySocket.send(JSON.stringify({ type: 'set_settings', ...newSettings }));

  let brightnessTimer;
  getElement('#range-br').oninput = (event) => {
    const value = Number(event.target.value);
    getElement('#br-val').textContent = `${value}%`;
    clearTimeout(brightnessTimer);
    brightnessTimer = setTimeout(() => updateSettings({ brightness: value }), 90);
  };

  function syncBrightnessSlider() {
    const slider = getElement('#range-br');
    const value = AppState.settings.brightness || 100;
    if (document.activeElement !== slider) {
      slider.value = value;
    }
    getElement('#br-val').textContent = `${value}%`;
  }

  async function loadProjectState() {
    try {
      const stateData = await apiRequest('state/');
      Object.assign(AppState, stateData);
      applyPermissions();
      renderAllPanels();
    } catch (error) {
      showToast(error.message);
    }
  }

  function renderAllPanels() {
    renderModels();
    renderControlFunctions();
    renderMembers();
    renderDetectedObjects();
    setPlaceholderState();
    updateControlButtons();
  }

  const getActiveModel = () =>
    AppState.models.find((model) => model.id === AppState.settings.model_id);

  function renderModels() {
    const modelsContainer = getElement('#models');
    modelsContainer.innerHTML = '';

    if (!AppState.models.length) {
      modelsContainer.innerHTML = '<div class="hint">No models yet. Upload a .tflite below.</div>';
    }

    AppState.models.forEach((model) => {
      const row = document.createElement('div');
      row.className =
        'mrow' + (model.id === AppState.settings.model_id ? ' sel' : '');
      row.innerHTML = `${getIconSvg('box')}<div class="grow"><b>${escapeHtml(model.name)}</b><small>input ${model.input} · ${escapeHtml(model.dtype)} · grid ${model.grid} · ${model.size_kb} KB</small></div>`;

      row.onclick = () => {
        if (AppState.permissions.control) {
          updateSettings({ model_id: model.id });
        } else {
          showToast('You are not allowed to change the model');
        }
      };
      modelsContainer.append(row);
    });

    const activeModel = getActiveModel();
    const labSection = getElement('#labsec');
    labSection.hidden = !activeModel;

    if (!activeModel) return;
    if (getElement('#labels').contains(document.activeElement)) return;

    const labelsContainer = getElement('#labels');
    labelsContainer.innerHTML = '';

    activeModel.labels.forEach((label) => {
      const row = document.createElement('div');
      row.className = 'lab';
      row.innerHTML = `<input type="color" value="${escapeHtml(label.color)}" aria-label="Color"><input type="text" value="${escapeHtml(label.name)}" aria-label="Name">
        <textarea placeholder="Object description" aria-label="Description">${escapeHtml(label.description)}</textarea>`;
      labelsContainer.append(row);
    });

    getElements('#labels input, #labels textarea').forEach(
      (field) => (field.disabled = !AppState.permissions.upload)
    );
  }

  getElement('#btn-savelabels').onclick = async () => {
    const activeModel = getActiveModel();
    if (!activeModel) return;

    const labels = getElements('#labels .lab').map((row) => ({
      color: row.children[0].value,
      name: row.children[1].value,
      description: row.children[2].value,
    }));

    try {
      await apiRequest(`models/${activeModel.id}/update/`, { json: { labels } });
      showToast('Descriptions saved');
      loadProjectState();
    } catch (error) {
      showToast(error.message);
    }
  };

  getElement('#btn-delmodel').onclick = async () => {
    const activeModel = getActiveModel();
    if (!activeModel || !confirm(`Delete model “${activeModel.name}”?`)) return;

    try {
      await apiRequest(`models/${activeModel.id}/delete/`, { method: 'POST', json: {} });
    } catch (error) {
      showToast(error.message);
    }
  };

  getElement('#btn-unload').onclick = () => updateSettings({ model_id: null });

  getElement('#btn-upload').onclick = async () => {
    const file = getElement('#up-file').files[0];
    if (!file) return showToast('Choose a .tflite file');

    const formData = new FormData();
    formData.append('file', file);
    formData.append('name', getElement('#up-name').value);
    formData.append('labels', getElement('#up-labels').value);

    const uploadButton = getElement('#btn-upload');
    uploadButton.disabled = true;

    try {
      const response = await apiRequest('models/upload/', { body: formData });
      showToast(response.warning || 'Model uploaded');
      getElement('#up-file').value = '';
      getElement('#up-name').value = '';
      getElement('#up-labels').value = '';
    } catch (error) {
      showToast(error.message);
    } finally {
      uploadButton.disabled = false;
    }
  };

  function renderDetectedObjects() {
    const activeModel = getActiveModel();
    const detectionsBox = getElement('#now');
    const legendBox = getElement('#legend');

    const countEntries = Object.entries(AppState.objectCounts || {});
    const getLabelInfo = (name) =>
      activeModel?.labels.find((l) => l.name === name) || {};

    detectionsBox.className = countEntries.length ? '' : 'hint';
    detectionsBox.innerHTML = countEntries.length
      ? countEntries
          .map(([name, count]) => {
            const label = getLabelInfo(name);
            const uniqueInfo = AppState.uniqueCounts[name]
              ? ` <small>(unique: ${AppState.uniqueCounts[name]})</small>`
              : '';
            return `<div class="user"><span class="dot" style="background:${escapeHtml(label.color || '#999')}"></span><div><b>${escapeHtml(name)} × ${count}</b>${uniqueInfo}<div class="hint">${escapeHtml(label.description || '')}</div></div></div>`;
          })
          .join('')
      : 'Nothing detected';

    legendBox.hidden = !countEntries.length;
    legendBox.innerHTML = countEntries
      .map(([name, count]) => {
        const label = getLabelInfo(name);
        const uniqueInfo = AppState.uniqueCounts[name]
          ? ` · unique ${AppState.uniqueCounts[name]}`
          : '';
        return `<div><i style="background:${escapeHtml(label.color || '#999')}"></i>${escapeHtml(name)} × ${count}${uniqueInfo}</div>`;
      })
      .join('');
  }

  const LIMIT_FIELDS = [
    ['ram', 'ram_mb', ' MB'],
    ['cpu', 'cpu_percent', '%'],
    ['cores', 'cpu_cores', ''],
    ['to', 'timeout_s', ' s'],
  ];

  const updateRamWarning = (val) => {
    getElement('#lim-ram-w').textContent =
      val < 30 ? '(below ≈ 30 MB the model will not start)' : '';
  };

  function renderLimits() {
    const limits = AppState.settings.limits || {};
    getElement('#lim-ram').max = AppState.systemCapabilities.max_ram_mb;
    getElement('#lim-cores').max = AppState.systemCapabilities.cpu_count;

    LIMIT_FIELDS.forEach(([fieldId, settingsKey, unit]) => {
      const inputElement = getElement('#lim-' + fieldId);
      if (document.activeElement !== inputElement && limits[settingsKey] != null) {
        inputElement.value = limits[settingsKey];
      }
      getElement(`#lim-${fieldId}-v`).textContent = inputElement.value + unit;
    });

    updateRamWarning(+getElement('#lim-ram').value);
  }

  LIMIT_FIELDS.forEach(([fieldId, , unit]) => {
    getElement('#lim-' + fieldId).oninput = (event) => {
      getElement(`#lim-${fieldId}-v`).textContent = event.target.value + unit;
      if (fieldId === 'ram') {
        updateRamWarning(+event.target.value);
      }
    };
  });

  getElement('#btn-limits').onclick = () =>
    updateSettings({
      limits: {
        ram_mb: +getElement('#lim-ram').value,
        cpu_percent: +getElement('#lim-cpu').value,
        cpu_cores: +getElement('#lim-cores').value,
        timeout_s: +getElement('#lim-to').value,
      },
    });

  function renderControlFunctions() {
    renderLimits();
    const settings = AppState.settings;

    getElement('#sw-unique').checked = Boolean(settings.unique);
    getElement('#sw-number').checked = Boolean(settings.numbering);

    const thresholdPercent = Math.round(settings.threshold * 100);
    getElement('#range-th').value = thresholdPercent;
    getElement('#th-val').textContent = `${thresholdPercent}%`;
  }

  getElement('#sw-unique').onchange = (e) => updateSettings({ unique: e.target.checked });
  getElement('#sw-number').onchange = (e) => updateSettings({ numbering: e.target.checked });
  getElement('#range-th').oninput = (e) => (getElement('#th-val').textContent = `${e.target.value}%`);
  getElement('#range-th').onchange = (e) => updateSettings({ threshold: e.target.value / 100 });
  getElement('#btn-reset-num').onclick = () =>
    telemetrySocket.send(JSON.stringify({ type: 'reset_tracker' }));

  getElement('#sw-desc').onchange = (e) => {
    AppState.showDescription = e.target.checked;
    drawOverlay();
  };

  getElement('#sw-perf').onchange = (e) => {
    getElement('#perf').hidden = !e.target.checked;
    getElement('#hud').hidden = !e.target.checked || getElement('#hud').textContent === '';
    fitStageLayout();
  };

  const PERMISSION_KEYS = [
    ['control', 'control'],
    ['draw', 'drawing'],
    ['stream', 'video'],
    ['upload', 'models'],
  ];
  const expandedUserPanels = new Set();

  function renderMembers() {
    const membersBox = getElement('#members');
    membersBox.innerHTML = '';
    getElement('#mcount').textContent = AppState.members.length || '';

    AppState.members.forEach((member) => {
      const isOnline = AppState.onlineUsers.includes(member.username);
      const isEditable = AppState.permissions.owner && member.role !== 'owner';

      const userHeaderHtml = `<span class="dot ${isOnline ? 'on' : ''}" title="${isOnline ? 'online' : 'offline'}"></span>${getIconSvg(member.role === 'owner' ? 'crown' : 'user')}<b class="grow">${escapeHtml(member.username)}${member.username === AppState.currentUser ? ' <small>(you)</small>' : ''}</b><small>${member.role === 'owner' ? 'owner' : 'member'}</small>`;

      const userElement = document.createElement(isEditable ? 'details' : 'div');
      userElement.className = 'user';

      if (isEditable) {
        userElement.open = expandedUserPanels.has(member.user_id);
        userElement.ontoggle = () =>
          userElement.open
            ? expandedUserPanels.add(member.user_id)
            : expandedUserPanels.delete(member.user_id);

        const permissionsCheckboxes = PERMISSION_KEYS.map(
          ([key, label]) =>
            `<label><input type="checkbox" data-u="${member.user_id}" data-k="${key}" ${member.perms[key] ? 'checked' : ''}>${label}</label>`
        ).join('');

        userElement.innerHTML = `<summary>${userHeaderHtml}</summary><div class="perm">${permissionsCheckboxes}</div><button class="btn sm danger" data-rm="${member.user_id}" style="margin-top:8px">${getIconSvg('trash')}Remove from project</button>`;
      } else {
        userElement.innerHTML = userHeaderHtml;
      }

      membersBox.append(userElement);
    });

    getElements('#members [data-k]').forEach(
      (checkbox) =>
        (checkbox.onchange = () =>
          apiRequest(`members/${checkbox.dataset.u}/update/`, {
            json: { [checkbox.dataset.k]: checkbox.checked },
          }).catch((err) => showToast(err.message)))
    );

    getElements('#members [data-rm]').forEach(
      (button) =>
        (button.onclick = () =>
          confirm('Remove this member?') &&
          apiRequest(`members/${button.dataset.rm}/remove/`, {
            method: 'POST',
            json: {},
          }).catch((err) => showToast(err.message)))
    );

    const invitesBox = getElement('#invites');
    invitesBox.innerHTML = AppState.invites.length
      ? ''
      : '<div class="hint">No active invites</div>';

    AppState.invites.forEach((invite) => {
      const inviteElement = document.createElement('div');
      inviteElement.className = 'user';

      const usageText = `used ${invite.uses}${invite.max_uses ? '/' + invite.max_uses : ''}`;
      const expirationText = invite.expires_at
        ? ' · until ' + new Date(invite.expires_at).toLocaleString('en-GB')
        : '';

      inviteElement.innerHTML = `${getIconSvg(invite.kind === 'device' ? 'phone' : 'key')}<div class="grow"><b>${escapeHtml(invite.code)}</b><small style="display:block;color:var(--mute)">${invite.kind === 'device' ? 'camera' : 'member'} · ${usageText}${expirationText}</small></div>
        <button class="btn sm icon" data-qr="${invite.id}" title="Show QR">${getIconSvg('qr')}</button><button class="btn sm icon danger" data-rv="${invite.id}" title="Revoke">${getIconSvg('x')}</button>`;

      invitesBox.append(inviteElement);
    });

    getElements('#invites [data-qr]').forEach(
      (button) =>
        (button.onclick = () =>
          showQrDialog(AppState.invites.find((i) => i.id == button.dataset.qr)))
    );

    getElements('#invites [data-rv]').forEach(
      (button) =>
        (button.onclick = () =>
          apiRequest(`invites/${button.dataset.rv}/revoke/`, {
            method: 'POST',
            json: {},
          }).catch((err) => showToast(err.message)))
    );
  }

  function showQrDialog(invite) {
    getElement('#dqr-title').textContent =
      invite.kind === 'device' ? 'Connect a phone' : 'Project invite';
    getElement('#dqr-img').src = invite.qr;
    getElement('#dqr-url').textContent = invite.url;
    getElement('#dqr-code').textContent = invite.code;
    getElement('#dqr').showModal();
  }

  getElement('#dqr-copy').onclick = () =>
    navigator.clipboard?.writeText(getElement('#dqr-url').textContent).then(() => showToast('Copied'));

  async function createInvite(payload) {
    try {
      const response = await apiRequest('invites/create/', { json: payload });
      showQrDialog({ ...response, kind: payload.kind });
      loadProjectState();
    } catch (error) {
      showToast(error.message);
    }
  }

  getElement('#btn-phone').onclick = getElement('#btn-phone2').onclick = () =>
    createInvite({ kind: 'device', hours: 4 });

  getElement('#btn-invite').onclick = () =>
    createInvite({
      kind: 'member',
      hours: +getElement('#iv-hours').value,
      max_uses: +getElement('#iv-max').value,
      control: getElement('#iv-control').checked,
      draw: getElement('#iv-draw').checked,
      stream: getElement('#iv-stream').checked,
      upload: getElement('#iv-upload').checked,
    });

  applyPermissions();
  setPlaceholderState();
  fitStageLayout();
  loadProjectState();
  updateConnectionStatus();

  Object.values(charts).forEach((sparkline) => sparkline.draw());
})();

