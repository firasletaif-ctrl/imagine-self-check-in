# Imagine Self Check-in

Borne tactile d’inscription et de check-in, pensée pour iPad et écran de kiosque. L’interface est disponible en français, anglais et arabe (RTL). Les visiteurs peuvent retrouver une inscription existante avec leur e-mail ou leur code de badge, ou s’inscrire sur place. Les arrivées sont enregistrées dans **Imagine Desk**.

## Fonctionnement et sécurité

Le navigateur de la borne ne contacte jamais directement Imagine Desk et ne reçoit jamais ses identifiants. Le petit serveur Python de ce projet utilise un compte d’équipe dédié, conservé uniquement dans ses variables d’environnement, puis appelle l’API existante d’Imagine Desk. L’API du Desk permet déjà de lire l’événement et les inscrits, de créer un inscrit et de confirmer une arrivée : aucune modification du dépôt `imagine-desk` n’est nécessaire pour ce MVP.

**Créer dans Imagine Desk un compte agent dédié à la borne**, avec un mot de passe unique, pour les actions courantes. Ne mettez jamais ses identifiants dans un fichier versionné, dans le navigateur ou sur l’iPad. Servez les deux projets en HTTPS pour un vrai événement. L’inscription sur place attribue la catégorie `Participant` et exige un nom, un e-mail et le consentement affiché. Pour un événement gratuit, l’arrivée est confirmée immédiatement. Pour un événement payant, l’inscription reste « Non payé » jusqu’à la confirmation de l’agent dans Imagine Desk, mais la borne enregistre l’arrivée dès l’inscription et oriente la personne vers le point de règlement. Elle n’a pas à revenir à la borne. L’organisation est facultative. Un e-mail déjà présent est renvoyé vers le parcours des inscrits afin d’éviter les doublons.

Une **plateforme d’administration** est disponible à `/admin`. Connectez-vous avec un compte ayant le rôle administrateur dans Imagine Desk. Vous pouvez choisir l’événement actif présenté sur la borne, modifier son nom, sa date et son lieu (les changements sont aussi écrits dans Imagine Desk), ouvrir/fermer les inscriptions, choisir si un règlement est requis et fixer une capacité (0 = illimitée). Les tarifs sont lus depuis la grille du Desk; l’agent choisit le tarif et confirme le paiement directement dans Imagine Desk.

Pour utiliser plusieurs bornes indépendantes, ouvrez des liens distincts comme `/1` et `/2`, puis configurez-les chacune dans `/admin/1` et `/admin/2`. Chaque identifiant conserve son propre événement et ses propres réglages de borne; le lien `/` et `/admin` restent disponibles pour la borne par défaut. Les participants et arrivées restent naturellement enregistrés dans l’événement Imagine Desk choisi. Les pièces jointes de programme sont partagées au niveau de l’événement. Pour garder ces réglages entre redémarrages, Render doit utiliser la base PostgreSQL du service.

La borne n’accepte **aucun paiement en ligne**. Pour un événement payant, une nouvelle inscription est créée avec le statut « Non payé »; pour un événement gratuit, elle est « Exonérée ». L’admin peut écrire le texte montré après une nouvelle inscription, une arrivée payée/non payée/exonérée, une arrivée déjà enregistrée, un inscrit dont le règlement reste à confirmer, une inscription introuvable, un doublon ou un événement complet. Les textes par défaut orientent vers la gauche ou la droite; l’admin peut personnaliser ces consignes.

Les réglages de la borne sont conservés dans PostgreSQL lorsque `SELF_CHECKIN_DATABASE_URL` est défini; sinon, ils sont enregistrés dans `data/kiosk_settings.json`. Le mot de passe administrateur n’est pas enregistré par la borne; la session d’administration reste en mémoire et expire au redémarrage du serveur.

### E-mail récapitulatif après paiement confirmé

Dans `/admin`, vous pouvez personnaliser l’objet et le texte du courriel et envoyer un e-mail de test. Le courriel réel ne part **qu’après que l’agent a marqué le paiement « Payé » dans Imagine Desk** (pas à la simple inscription). Le serveur vérifie le statut auprès du Desk toutes les 30 secondes et protège l’envoi contre les doublons. Les paiements déjà confirmés avant l’activation des courriels ne sont pas envoyés rétroactivement. Le vérificateur tourne dans le service Web : si le plan Render choisi suspend le service lorsqu’il est inactif, l’envoi peut attendre son prochain réveil. Pour un envoi ponctuel, utilisez un plan qui maintient le service actif.

Variables disponibles dans le modèle : `{{name}}`, `{{price}}`, `{{amount}}`, `{{currency}}`, `{{participant_id}}`, `{{code}}`, `{{event_name}}`, `{{event_date}}`, `{{venue}}`, `{{tariff}}`, `{{payment_method}}` et `{{payment_reference}}`. Le texte reste un courriel texte brut. Dans `/admin`, vous pouvez joindre jusqu’à cinq fichiers PDF, JPG, PNG ou WebP par événement (5 Mo par fichier, 12 Mo au total); ils sont envoyés en pièces jointes avec l’e-mail de confirmation et l’e-mail de test. Les fichiers et les identifiants d’envoi sont conservés dans la base PostgreSQL de la borne.

Pour activer cette fonction, ajoutez dans les secrets Render les variables SMTP : `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM` et `SMTP_USE_TLS=1`. Le port 587 avec STARTTLS est courant; le port 465 utilise automatiquement TLS implicite. N’activez ensuite l’envoi dans `/admin` qu’après avoir utilisé **Envoyer un e-mail de test**. Les identifiants SMTP ne sont jamais saisis dans l’admin ni envoyés au navigateur.

La borne peut répartir les e-mails entre deux comptes SMTP autorisés pour l’événement. Configurez le compte prioritaire avec les variables `SMTP_*` ci-dessus, puis le second avec `SMTP_SECONDARY_HOST`, `SMTP_SECONDARY_PORT`, `SMTP_SECONDARY_USERNAME`, `SMTP_SECONDARY_PASSWORD`, `SMTP_SECONDARY_FROM` et `SMTP_SECONDARY_USE_TLS=1`. Les variables `SMTP_PRIMARY_DAILY_LIMIT` et `SMTP_SECONDARY_DAILY_LIMIT` définissent le nombre maximal de tentatives par compte et par journée (300 par défaut); `SMTP_QUOTA_TIMEZONE` détermine le changement de journée (par défaut `Africa/Tunis`). PostgreSQL est requis pour conserver les compteurs entre redémarrages. La borne utilise d’abord le premier compte, puis le second quand le plafond local du premier est atteint; elle revient au premier au prochain jour configuré. Les e-mails de test comptent aussi. Ces compteurs ne voient que les envois effectués par cette borne : si d’autres applications utilisent ces comptes, réduisez les plafonds en conséquence ou gardez les comptes dédiés, sinon le total réel chez Brevo peut dépasser le quota avant le basculement.

Le serveur n’expose pas la liste des participants au navigateur : il ne renvoie que le résultat de la recherche exacte par e-mail/code. Il impose aussi une limitation de tentatives et n’écrit pas les données personnelles dans ses journaux HTTP. Pour une exploitation professionnelle, placez-le derrière un reverse proxy HTTPS et ajoutez une politique de confidentialité adaptée à votre événement.

## Configuration

Python 3.10 ou plus récent. Le mode local avec fichier JSON n’a pas de dépendance supplémentaire; PostgreSQL utilise `psycopg` (installé par `requirements.txt`) :

```bash
export IMAGINE_DESK_URL="https://votre-desk.example.tn"
export IMAGINE_DESK_USERNAME="kiosk.agent"
export IMAGINE_DESK_PASSWORD="mot-de-passe-unique-et-long"
# Facultatif au premier lancement : l’événement se choisit ensuite dans /admin
export IMAGINE_DESK_EVENT_ID="identifiant-de-l-evenement"
python server.py
```

`IMAGINE_DESK_EVENT_ID` est facultatif : s’il est défini, il sert d’événement initial. Sinon, après lancement, ouvrez `/admin` et choisissez un événement. Pour la première connexion à `/admin`, utilisez un **compte administrateur Imagine Desk**; le compte agent configuré pour la borne ne peut pas modifier les événements. Ne mettez pas de vrais identifiants dans un `.env` versionné.

La borne est alors accessible sur `http://adresse-du-serveur:8000` et l’administration sur `http://adresse-du-serveur:8000/admin`. L’application refuse une URL Desk non sécurisée, sauf `localhost` pour le développement. Les variables `HOST` (défaut `0.0.0.0`) et `PORT` (défaut `8000`) règlent l’écoute locale. Pour Internet, ajoutez un hébergement HTTPS; le serveur de développement intégré n’est pas un serveur de production.

### Déploiement en conteneur / plateforme

Définissez `IMAGINE_DESK_URL`, `IMAGINE_DESK_USERNAME` et `IMAGINE_DESK_PASSWORD` dans le panneau **Secrets / Environment** de l’hébergeur, et laissez `PORT` fourni par la plateforme. Ajoutez aussi les variables SMTP ci-dessus si vous souhaitez envoyer les récapitulatifs de paiement. `IMAGINE_DESK_EVENT_ID` est facultatif. Commande de démarrage : `python server.py`. Ne copiez pas les identifiants dans la configuration du navigateur ou dans les paramètres du raccourci iPad. Le compte agent doit être actif et avoir accès à l’événement choisi. Le Blueprint `render.yaml` crée un service Web et une petite base PostgreSQL séparée, utilisée uniquement pour conserver les réglages de la borne. Les données des participants restent dans Imagine Desk et sont manipulées via son API; elles ne sont pas dupliquées dans cette base. Le service Web doit recevoir `IMAGINE_DESK_URL`, `IMAGINE_DESK_USERNAME` et `IMAGINE_DESK_PASSWORD` dans **Environment → Secrets**; le compte agent doit être actif et autorisé à ouvrir une session. L’identifiant de l’événement se choisit dans `/admin`. Le compte administrateur Imagine Desk est saisi au moment de se connecter à `/admin`, jamais dans les variables du service.

Pour Render : créez un **Blueprint** depuis ce dépôt et déployez les deux ressources déclarées (`imagine-self-check-in` et `imagine-self-check-in-db`). Ajoutez ensuite les trois secrets d’Imagine Desk au service Web. Vérifiez que l’état de santé `/api/health` est vert, puis connectez-vous à `/admin`, choisissez l’événement et enregistrez les réglages. La base déclarée ici est séparée de celle du Desk; elle conserve les réglages de la borne, les pièces jointes des programmes et les identifiants d’envoi des courriels afin d’éviter les doublons. Vérifiez la durée de conservation et les limites du plan Render choisi avant un événement réel; utilisez un plan avec persistance adaptée et préparez des sauvegardes.

## Utilisation iPad / borne

1. Démarrer Imagine Desk et cette borne sur des adresses accessibles par le même réseau ou Internet.
2. Ouvrir l’adresse de la borne dans Safari ou Chromium. Il faut HTTPS si la borne est hébergée en ligne.
3. Sur iPad, Safari → **Partager → Sur l’écran d’accueil**, puis activer l’accès guidé pour verrouiller l’appareil sur la borne. Sur une borne Android/Windows, activer le mode kiosque du navigateur ou du système.
4. Tester avant l’événement : une inscription nouvelle, une recherche par e-mail, une recherche par code, un doublon, un retour arrière, et la synchronisation visible dans Imagine Desk.

## Périmètre du MVP / suites possibles

- Inscription sur place : nom, e-mail, organisation; événement gratuit = arrivée confirmée immédiatement, événement payant = arrivée enregistrée immédiatement et statut « Non payé » jusqu’à la confirmation de l’agent dans Imagine Desk. La personne se dirige directement au point de règlement, sans repasser à la borne.
- Check-in des préinscrits : recherche exacte par e-mail ou code et confirmation explicite. Un préinscrit non payé peut confirmer son arrivée en un seul passage à la borne, puis aller régler; son paiement reste non confirmé jusqu’à l’action de l’agent.
- Interface tactile responsive et langues FR / EN / AR.
- Aucun scan caméra/QR pour l’instant (saisie du code possible); aucune impression de badge depuis la borne.
- Le tarif et la confirmation de paiement restent gérés dans Imagine Desk. L’agent confirme le paiement; le courriel personnalisé est ensuite envoyé par SMTP.
- Les champs d’inscription supplémentaires (catégorie, téléphone, etc.) peuvent être adaptés au formulaire avant la production.
- La limitation de tentatives est en mémoire par processus : plusieurs réplicas derrière un proxy nécessiteraient une limite partagée au niveau du proxy.
- Le check-in et l’inscription nécessitent une connexion réseau au serveur Imagine Desk; prévoyez une procédure d’accueil de secours en cas de panne Internet.

L’aperçu local peut afficher le formulaire sans configuration. Pour l’utiliser réellement, déployez les ressources Render, configurez les trois secrets du compte agent, choisissez un événement dans `/admin` avec un compte administrateur Imagine Desk, puis vérifiez la connexion avant le jour de l’événement. Localement, `SELF_CHECKIN_DATABASE_URL` est facultatif : sans elle, la borne garde ses réglages dans le fichier JSON ignoré par Git.
