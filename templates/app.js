(function () {
  const difficultySelect = document.getElementById('difficulty-select');
  const pointsInput = document.getElementById('points-input');
  const mapping = {'початковий': 3, 'середній': 6, 'достатній': 9, 'високий': 12};
  if (difficultySelect && pointsInput) {
    difficultySelect.addEventListener('change', function () {
      if (mapping[this.value]) pointsInput.value = mapping[this.value];
    });
  }
})();
