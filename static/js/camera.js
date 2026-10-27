(() => {
  const cameraConfig = window.CAM;
  const videoElement = document.querySelector('#v');
  const overlayCanvas = document.querySelector('#o');
  const statusElement = document.querySelector('#st');
  const startButton = document.querySelector('#go');
  const stopButton = document.querySelector('#stop');
  const flipButton = document.querySelector('#flip');

  const offscreenCanvas = document.createElement('canvas');
  const offscreenContext = offscreenCanvas.getContext('2d');

  let stream = null;
  let facingMode = 'environment';
  let isFrameInFlight = false;
  let lastFrameSentAt = 0;
  let isRunning = false;
  let predictions = [];
  let webSocket = null;
  let reconnectDelayMs = 500;
  let isAccessClosed = false;
  let wakeLock = null;

  function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const socketUrl = `${protocol}://${location.host}/ws/stream/${cameraConfig.pid}/?t=${encodeURIComponent(cameraConfig.code)}`;

    webSocket = new WebSocket(socketUrl);
    webSocket.binaryType = 'arraybuffer';

    webSocket.onopen = () => {
      reconnectDelayMs = 500;
      isFrameInFlight = false;
      statusElement.textContent = 'Connected';
    };

    webSocket.onclose = (event) => {
      if (event.code === 4403) {
        isAccessClosed = true;
        stopStream();
        statusElement.textContent = 'Access closed (invalid code)';
        return;
      }

      statusElement.textContent = 'Connection lost — reconnecting…';
      isFrameInFlight = false;
      reconnectDelayMs = Math.min(reconnectDelayMs * 2, 6000);
      setTimeout(connectWebSocket, reconnectDelayMs);
    };

    webSocket.onerror = () => {
      statusElement.textContent = 'Could not connect to the server';
    };

    webSocket.onmessage = handleWebSocketMessage;
  }

  function handleWebSocketMessage(event) {
    if (typeof event.data === 'string') {
      const response = JSON.parse(event.data);
      if (response.type === 'ack') {
        isFrameInFlight = false;
      }
      return;
    }

    const dataView = new DataView(event.data);
    const headerLength = dataView.getUint32(0);
    const headerJsonText = new TextDecoder().decode(
      new Uint8Array(event.data, 4, headerLength)
    );
    const headerData = JSON.parse(headerJsonText);

    predictions = headerData.predictions || [];
    drawPredictions();

    if (headerData.error) {
      statusElement.textContent = headerData.error;
    } else if (headerData.stopped) {
      statusElement.textContent = 'Model stopped';
    } else {
      statusElement.textContent = `${headerData.inference_ms} ms · objects: ${predictions.length}`;
    }
  }

  function drawPredictions() {
    const cssWidth = overlayCanvas.clientWidth;
    const cssHeight = overlayCanvas.clientHeight;
    const pixelRatio = window.devicePixelRatio || 1;

    if (overlayCanvas.width !== cssWidth * pixelRatio) {
      overlayCanvas.width = cssWidth * pixelRatio;
      overlayCanvas.height = cssHeight * pixelRatio;
    }

    const context = overlayCanvas.getContext('2d');
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    context.clearRect(0, 0, cssWidth, cssHeight);

    const videoWidth = videoElement.videoWidth || 1;
    const videoHeight = videoElement.videoHeight || 1;

    const scale = Math.min(cssWidth / videoWidth, cssHeight / videoHeight);
    const offsetX = (cssWidth - videoWidth * scale) / 2;
    const offsetY = (cssHeight - videoHeight * scale) / 2;

    context.font = '600 13px sans-serif';

    for (const prediction of predictions) {
      const pointX = offsetX + prediction.x * videoWidth * scale;
      const pointY = offsetY + prediction.y * videoHeight * scale;

      context.beginPath();
      context.arc(pointX, pointY, 9, 0, 2 * Math.PI);
      context.fillStyle = prediction.color;
      context.fill();
      context.strokeStyle = '#fff';
      context.lineWidth = 2;
      context.stroke();

      const trackPrefix = prediction.track_id ? `#${prediction.track_id} ` : '';
      const confidencePercent = Math.round(prediction.confidence * 100);
      const labelText = `${trackPrefix}${prediction.label} ${confidencePercent}%`;

      const textMetrics = context.measureText(labelText);
      context.fillStyle = '#000a';
      context.fillRect(pointX + 12, pointY - 11, textMetrics.width + 8, 20);

      context.fillStyle = '#fff';
      context.fillText(labelText, pointX + 16, pointY + 4);
    }
  }

  function stopStream() {
    isRunning = false;

    if (stream) {
      stream.getTracks().forEach((track) => track.stop());
      stream = null;
    }

    videoElement.srcObject = null;
    predictions = [];
    drawPredictions();

    if (wakeLock) {
      wakeLock.release().catch(() => {});
      wakeLock = null;
    }

    startButton.disabled = false;
  }

  async function startStream() {
    if (!navigator.mediaDevices?.getUserMedia) {
      statusElement.textContent = 'HTTPS is required for camera access';
      return;
    }

    if (stream) {
      stream.getTracks().forEach((track) => track.stop());
    }

    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: {
          facingMode: facingMode,
          width: { ideal: 640 },
          height: { ideal: 480 }
        },
        audio: false
      });
    } catch (error) {
      statusElement.textContent = `Could not open the camera: ${error.message}`;
      return;
    }

    videoElement.srcObject = stream;
    try {
      await new Promise((resolve, reject) => {
        if (videoElement.readyState >= HTMLMediaElement.HAVE_METADATA) {
          resolve();
          return;
        }
        videoElement.addEventListener('loadedmetadata', resolve, { once: true });
        videoElement.addEventListener('error', () => reject(new Error('Could not read the camera stream')), { once: true });
      });
      await videoElement.play();
    } catch (error) {
      stopStream();
      statusElement.textContent = `Could not start the camera: ${error.message}`;
      return;
    }

    if (!videoElement.videoWidth || !videoElement.videoHeight) {
      stopStream();
      statusElement.textContent = 'The camera returned no video frames';
      return;
    }

    if (!isRunning) {
      isRunning = true;
      statusElement.textContent = 'Sending video…';
      processFrameLoop();
    }

    try {
      wakeLock = await navigator.wakeLock.request('screen');
    } catch {}
  }

  function processFrameLoop() {
    if (!isRunning) return;

    const isSocketOpen = webSocket && webSocket.readyState === WebSocket.OPEN;
    const hasTimeoutPassed = Date.now() - lastFrameSentAt > 3000;

    if (isSocketOpen && videoElement.videoWidth && (!isFrameInFlight || hasTimeoutPassed)) {
      const maxDimension = Math.max(videoElement.videoWidth, videoElement.videoHeight);
      const scale = Math.min(1, 480 / maxDimension);

      offscreenCanvas.width = videoElement.videoWidth * scale;
      offscreenCanvas.height = videoElement.videoHeight * scale;

      offscreenContext.drawImage(
        videoElement,
        0,
        0,
        offscreenCanvas.width,
        offscreenCanvas.height
      );

      isFrameInFlight = true;
      lastFrameSentAt = Date.now();

      offscreenCanvas.toBlob((blob) => {
        if (!blob || !webSocket || webSocket.readyState !== WebSocket.OPEN) {
          isFrameInFlight = false;
          return;
        }
        blob.arrayBuffer()
          .then((buffer) => {
            if (webSocket && webSocket.readyState === WebSocket.OPEN) {
              webSocket.send(buffer);
            } else {
              isFrameInFlight = false;
            }
          })
          .catch(() => {
            isFrameInFlight = false;
            statusElement.textContent = 'Could not encode the camera frame';
          });
      }, 'image/jpeg', 0.7);
    }

    setTimeout(processFrameLoop, 40);
  }

  connectWebSocket();

  startButton.onclick = startStream;
  stopButton.onclick = () => {
    stopStream();
    statusElement.textContent = 'Stream stopped';
  };
  flipButton.onclick = () => {
    facingMode = facingMode === 'environment' ? 'user' : 'environment';
    startStream();
  };

  window.addEventListener('resize', drawPredictions);
})();
