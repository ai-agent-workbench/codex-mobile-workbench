(function () {
  'use strict';

  const button = document.querySelector('.fingerprint-action');
  const label = document.querySelector('.action-label');
  const mode = document.body.dataset.mode || 'login';
  const originalLabel = document.body.dataset.action || '轻触验证';
  const returnTo = document.body.dataset.returnTo || '';

  function decode(value) {
    const padded = value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - value.length % 4) % 4);
    const raw = atob(padded);
    return Uint8Array.from(raw, char => char.charCodeAt(0));
  }

  function encode(value) {
    if (value === null || value === undefined) return null;
    const bytes = new Uint8Array(value);
    let raw = '';
    bytes.forEach(byte => { raw += String.fromCharCode(byte); });
    return btoa(raw).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
  }

  function creationOptions(options) {
    options.challenge = decode(options.challenge);
    options.user.id = decode(options.user.id);
    (options.excludeCredentials || []).forEach(item => { item.id = decode(item.id); });
    return options;
  }

  function requestOptions(options) {
    options.challenge = decode(options.challenge);
    (options.allowCredentials || []).forEach(item => { item.id = decode(item.id); });
    return options;
  }

  function credentialJSON(credential) {
    const response = credential.response;
    const output = {
      id: credential.id,
      rawId: encode(credential.rawId),
      type: credential.type,
      authenticatorAttachment: credential.authenticatorAttachment,
      response: {
        clientDataJSON: encode(response.clientDataJSON),
      },
    };
    if ('attestationObject' in response) {
      output.response.attestationObject = encode(response.attestationObject);
      output.response.transports = response.getTransports ? response.getTransports() : [];
    } else {
      output.response.authenticatorData = encode(response.authenticatorData);
      output.response.signature = encode(response.signature);
      output.response.userHandle = encode(response.userHandle);
    }
    return output;
  }

  async function post(path, data) {
    const response = await fetch(path, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data || {}),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || 'request_failed');
    return payload;
  }

  async function register() {
    let proof;
    if (mode === 'recover') {
      proof = window.prompt('输入本机恢复码');
      if (!proof) return;
    } else {
      proof = window.prompt('输入当前动态验证码');
      if (!proof) return;
    }
    const body = mode === 'recover' ? {recoveryCode: proof} : {code: proof};
    const options = creationOptions(await post('/auth/passkey/register/options', body));
    const credential = await navigator.credentials.create({publicKey: options});
    if (!credential) throw new Error('credential_missing');
    return post('/auth/passkey/register/finish', {credential: credentialJSON(credential), return_to: returnTo});
  }

  async function authenticate() {
    const options = requestOptions(await post('/auth/passkey/login/options', {}));
    const credential = await navigator.credentials.get({publicKey: options});
    if (!credential) throw new Error('credential_missing');
    return post('/auth/passkey/login/finish', {credential: credentialJSON(credential), return_to: returnTo});
  }

  button.addEventListener('click', async function () {
    if (!window.PublicKeyCredential || button.disabled) {
      label.textContent = '当前浏览器不可用';
      return;
    }
    button.disabled = true;
    label.textContent = mode === 'login' ? '正在验证…' : '正在绑定…';
    try {
      const result = mode === 'login' ? await authenticate() : await register();
      window.location.assign(result.redirect || '/home');
    } catch (error) {
      if (error && error.name === 'NotAllowedError') {
        label.textContent = originalLabel;
      } else {
        label.textContent = '验证失败，请重试';
      }
      button.disabled = false;
    }
  });
}());
