---

title: "Paul Caillon"
description: "Paul Caillon is an Associate Professor at Université Paris Dauphine – PSL working on frugal and efficient deep learning."
---

<header class="intro" id="top" aria-labelledby="intro-title">
  <div class="intro-meta">
    <div class="intro-profile">
      <img
        class="intro-portrait"
        src="{{ '/assets/img/paul-caillon.jpeg' | relative_url }}"
        alt="Portrait of Paul Caillon"
        width="225"
        height="225"
      >
    </div>

<div class="intro-links">
  <nav class="institution-logos" aria-label="Academic affiliations">
    <a class="affiliation-logo dauphine-logo" href="{{ site.author.affiliation_url }}"><img src="{{ '/assets/img/logo-dauphine.jpg' | relative_url }}" alt="Université Paris Dauphine – PSL" width="759" height="208"></a>
    <a class="affiliation-logo miles-logo" href="{{ site.author.team_url }}"><img src="{{ '/assets/img/logo-miles.png' | relative_url }}" alt="MILES — Machine Intelligence and Learning Systems" width="2048" height="511"></a>
  </nav>
</div>

<div class="intro-copy">
  <h1 class="intro-greeting" id="intro-title">Hello, I am <strong>Paul Caillon</strong>, an Associate Professor at Université Paris Dauphine – PSL. Welcome to my homepage!</h1>
  <p class="research-statement">
    I work on the foundations of frugal deep learning: how can neural networks learn and process information with less computation? My research combines theoretical analysis with the design of more efficient learning algorithms and neural architectures, from alternatives to backpropagation to efficient attention mechanisms.
  </p>
</div>

  </div>
</header>

<main id="main-content" tabindex="-1">

  <section class="section news" id="news" aria-labelledby="news-title">
    <div class="section-heading news-heading">
      <h2 id="news-title">News</h2>
      <!-- <p>Recent announcements and highlights.</p> -->
    </div>

<ul class="news-list">
  {% assign recent_news = site.data.news | sort: "date" | reverse %}
  {% for item in recent_news limit: 4 %}
  <li class="news-item">
    <time class="news-date" datetime="{{ item.date }}">{{ item.display_date | escape }}</time>
    {{ item.lead | escape }}
    {% if item.url and item.link_text %}
    <a href="{{ item.url | escape }}">{{ item.link_text | escape }}</a>
    {% elsif item.url and item.title %}
    <a href="{{ item.url | escape }}">“{{ item.title | escape }}”</a>
    {% endif %}
    {{ item.tail | escape }}
  </li>
  {% endfor %}
</ul>

  </section>

  <section class="section split-section" id="research" aria-labelledby="research-title">
    <h2 id="research-title">Research</h2>

<div class="section-copy">
  <p>
    My research focuses on making deep learning more computationally efficient by rethinking how neural networks learn and process information. I am particularly interested in efficient learning algorithms and neural architectures, and in how gradients and information propagate through deep networks.
  </p>

  <p>
    I investigate how deep networks can be trained more efficiently, how attention mechanisms can handle long sequences at lower computational cost, and how architectures can adapt their capacity during learning. More broadly, I explore how models can learn effectively from limited data and supervision. These questions connect my work on language models and scientific machine learning, with an emphasis on understanding the trade-offs between efficiency, generalization, and robustness.
  </p>
  <p>
    From 2019 to 2023, I was a PhD student at
    <a href="https://www.loria.fr/">LORIA</a>
    under the supervision of
    <a href="https://members.loria.fr/CCerisara/">Christophe Cerisara</a>.
    I then joined LAMSADE as a postdoctoral researcher from 2023 to 2025, working with
    <a href="https://allauzen.github.io/">Alexandre Allauzen</a>.
    From 2025 to 2026, I was a PSL AI Fellow. Since the 1st of September 2026, I am an Associate Professor at Université Paris Dauphine – PSL.
  </p>

    <div class="supervision-copy" id="supervision">
      <hr class="supervision-divider">
      <p>I also enjoy working with PhD students and research interns. Current and former students I have supervised or co-supervised include:</p>
      <ul aria-label="Current students and interns">
        <li><a href="https://dauphine.psl.eu/en/research/resume-database/profile/zhao-hangyue">Hangyue Zhao</a> — former intern, now a PhD student co-supervised with
        <a href="https://allauzen.github.io/">Alexandre Allauzen.</a></li>
        <li><a href="https://www.lamsade.dauphine.fr/en/people/detail-cv/profile/alex-colagrande.html">Alex Colagrande</a> — former intern, now a PhD student co-supervised with Alexandre Allauzen.</li>
        <li><a href="https://erwanfagnou.github.io/">Erwan Fagnou</a> — PhD student, co-supervised with Alexandre Allauzen.</li>
        <li><a href="https://joeydavid.fyi/">Joey David</a> — research intern.</li>
      </ul>

      <hr class="supervision-divider">

      <ul aria-label="Former interns">
        <li>Nan An — former intern.</li>
        <li>Zakariae Moutaouakil — former intern, co-supervised with <a href="https://blaisedelattre.github.io/">Blaise Delattre</a>.</li>
      </ul>

      <h3>Interested in joining?</h3>
      <p>
        I welcome enquiries about research internships and PhD opportunities in frugal deep learning.
        If you are interested in working together, please <a href="mailto:{{ site.author.email }}">get in touch</a>
        with your CV and a short description of your research interests.
      </p>
    </div>
  </div>
  </section>

  <section class="section publications" id="publications" aria-labelledby="publications-title">
    {% assign current_year = site.time | date: "%Y" | plus: 0 %}
    {% assign publication_cutoff_year = current_year | minus: 2 %}

<div class="section-heading">
  <h2 id="publications-title">Publications</h2>
  <!-- <p>Recent publications from {{ publication_cutoff_year }} onward.</p> -->
</div>

{% assign published_publications = site.data.publications | where: "published", true %}
{% comment %}Keep the ICLR 2026 workshop paper beside its conference companion, independently of API record dates.{% endcomment %}
{% assign iclr_conference = published_publications | where: "title", "Scaling Direct Feedback Learning with Jacobian Alignment Guarantees" %}
{% assign iclr_workshop = published_publications | where: "title", "Limits of Resolution Equivariance in Fourier Neural Operators" %}
{% if iclr_conference.size > 0 and iclr_workshop.size > 0 %}
  {% assign ordered_publications = "" | split: "," %}
  {% for item in published_publications %}
    {% unless item.title == iclr_workshop.first.title %}
      {% assign entry = published_publications | where: "title", item.title %}
      {% assign ordered_publications = ordered_publications | concat: entry %}
      {% if item.title == iclr_conference.first.title %}
        {% assign ordered_publications = ordered_publications | concat: iclr_workshop %}
      {% endif %}
    {% endunless %}
  {% endfor %}
  {% assign published_publications = ordered_publications %}
{% endif %}

{% assign first_publication = published_publications | where: "title", site.data.publication-display.first_publication %}
{% assign remaining_publications = published_publications | where_exp: "item", "item.title != site.data.publication-display.first_publication" %}
{% assign published_publications = first_publication | concat: remaining_publications %}
<ul class="publication-list" role="list">
  {% for publication in published_publications %}
  {% assign publication_year = publication.year | plus: 0 %}

  {% if publication_year >= publication_cutoff_year %}
  {% assign conference = site.data.publication-display.conferences[publication.venue] %}
  {% assign details = site.data.publication-display.papers[publication.title] %}
  <li class="publication-list-item">
    <time
      class="publication-year"
      datetime="{{ conference.month | default: publication.year }}"
    >
      {{ publication.year }}
      {% if conference.month %}<span class="publication-month">{{ conference.month | append: "-01" | date: "%B" }}</span>{% endif %}
    </time>

    <p>
      {% assign publication_url = publication.venue_url | default: publication.doi | default: publication.arxiv | default: publication.pdf %}

      {% if publication_url %}
      <a
        class="publication-title"
        href="{{ publication_url | escape }}"
      >
        {{ publication.title | escape }}
      </a>
      {% else %}
      <span class="publication-title">
        {{ publication.title | escape }}
      </span>
      {% endif %}

      {% if publication.venue %}
      <span class="publication-venue">
        — {{ details.venue_label | default: publication.venue | escape }}
      </span>
      {% endif %}
      {% if details.award %}
      <strong class="publication-distinction">· {{ details.award | escape }}</strong>
      {% endif %}
      {% if details.presentation %}
      <strong class="publication-distinction">· {{ details.presentation | escape }}</strong>
      {% endif %}
      <span class="publication-authors">{% for author in publication.authors %}{% if author == site.author.name %}<span class="author-self" aria-label="{{ author | escape }}">P.C.</span>{% else %}{{ author | escape }}{% endif %}{% if details.equal_contributors contains author %}<sup aria-label="equal contribution">*</sup>{% endif %}{% unless forloop.last %}, {% endunless %}{% endfor %}</span>
    </p>
  </li>
  {% endif %}
  {% endfor %}
</ul>

<p class="publication-note"><span>*</span> Equal contribution.</p>
<p class="publication-note">
  For older publications and my full research record, see
  <a href="{{ site.author.scholar_url }}">
    Google Scholar <span aria-hidden="true">↗</span>
  </a>.
</p>

  </section>





</main>
