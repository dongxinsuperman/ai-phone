(function () {
  const query = new URLSearchParams(location.search);
  const key = 'personal-vm-e2e:' + (query.get('run') || 'missing-run');
  const result = {
    href: location.href,
    origin: location.origin,
    storageKey: key,
    runFromLocation: query.get('run'),
    platformFromLocation: query.get('platform'),
    userAgent: navigator.userAgent,
    documentReadyState: document.readyState,
    capturedDateMs: Date.now(),
    capturedPerformanceMs: performance.now(),
    rawValue: null,
    missing: null,
    readError: null,
    parseError: null,
    parsedValue: null
  };
  try {
    result.rawValue = localStorage.getItem(key);
    result.missing = result.rawValue === null;
  } catch (error) {
    result.readError = String(error && (error.stack || error.message) || error);
  }
  if (result.readError === null && result.rawValue !== null) {
    try {
      result.parsedValue = JSON.parse(result.rawValue);
    } catch (error) {
      result.parseError = String(error && (error.stack || error.message) || error);
    }
  }
  return JSON.stringify(result);
})()
