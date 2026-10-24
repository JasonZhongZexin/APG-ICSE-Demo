"use strict";

window.QueryLimits = (() => {
  const fields = [
    { id: "queryMaxDepth", key: "max_depth", maximum: null },
    { id: "queryMaxEntities", key: "max_entities", maximum: 1000 },
  ];

  function validate(input, maximum) {
    const value = input.value;
    const valid = (!input.validity.badInput && value === "") || (/^[0-9]+$/.test(value) && Number.isSafeInteger(Number(value)) && Number(value) >= 1 && (maximum === null || Number(value) <= maximum));
    const message = maximum === null ? "Enter a positive whole number." : `Enter a whole number between 1 and ${maximum}.`;
    input.setCustomValidity(valid ? "" : message);
    return valid;
  }

  function initialize() {
    fields.forEach(({ id, maximum }) => {
      const input = document.getElementById(id);
      input.addEventListener("keydown", (event) => {
        if (!event.ctrlKey && !event.metaKey && event.key.length === 1 && !/^[0-9]$/.test(event.key)) event.preventDefault();
      });
      input.addEventListener("beforeinput", (event) => {
        if (event.data && !/^[0-9]+$/.test(event.data)) event.preventDefault();
      });
      input.addEventListener("paste", (event) => {
        if (!/^[0-9]+$/.test(event.clipboardData.getData("text"))) event.preventDefault();
      });
      input.addEventListener("input", () => validate(input, maximum));
    });
  }

  function read() {
    const values = {};
    let valid = true;
    fields.forEach(({ id, key, maximum }) => {
      const input = document.getElementById(id);
      if (!validate(input, maximum)) valid = false;
      values[key] = input.value === "" ? null : Number(input.value);
    });
    if (!valid) {
      document.getElementById("queryForm").reportValidity();
      return null;
    }
    return values;
  }

  function setDisabled(disabled) {
    fields.forEach(({ id }) => { document.getElementById(id).disabled = disabled; });
  }

  return { initialize, read, setDisabled };
})();
