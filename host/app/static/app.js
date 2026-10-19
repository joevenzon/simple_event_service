// Keep UTC as a fallback, then show local calendar time and age without reading private APIs.
for (const element of document.querySelectorAll('time[datetime]')) {
  const instant = new Date(element.dateTime);
  if (Number.isNaN(instant.getTime())) continue;

  const seconds = Math.round((instant.getTime() - Date.now()) / 1000);
  const magnitude = Math.abs(seconds);
  let [unit, divisor] = ['second', 1];
  if (magnitude >= 86400) [unit, divisor] = ['day', 86400];
  else if (magnitude >= 3600) [unit, divisor] = ['hour', 3600];
  else if (magnitude >= 60) [unit, divisor] = ['minute', 60];

  const relative = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
    .format(Math.round(seconds / divisor), unit);
  const local = new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium', timeStyle: 'long'
  }).format(instant);
  element.textContent = `${local} (${relative})`;
  element.title = element.dateTime;
}
